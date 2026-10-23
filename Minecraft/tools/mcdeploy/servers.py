"""The server list lives in GitHub Environments, one per server.

A GitHub job only sees the secrets of its own environment, so the proxy cannot read the backend
environments directly. Instead every server's job ("collector") writes its Velocity entry
encrypted with the repository secret MC_COLLECT_KEY; the encrypted files travel as workflow
artifacts (public repo: only ciphertext is published) and the proxy job decrypts and checks them.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import CACHE_DIR, Layout, load_layout, require_values, server_name_for
from .errors import McDeployError, fail_if

HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
COLLECTED_SUFFIX = ".server.enc"
# Outside .cache/out (which every mc.sh call empties) so the upload step still finds it.
COLLECT_OUT_DIR = CACHE_DIR / "collected-out"
MAX_AGE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class GithubEnvironment:
    name: str
    protected: bool  # has required reviewers


def list_environments() -> list[GithubEnvironment]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise McDeployError("GITHUB_REPOSITORY is not set; cannot list GitHub Environments")
    found: list[GithubEnvironment] = []
    page = 1
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/environments?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise McDeployError(f"GitHub API error {exc.code} listing environments of {repo}") from None
        except urllib.error.URLError as exc:
            raise McDeployError(f"GitHub API unreachable: {exc.reason}") from None
        envs = data.get("environments") or []
        for env in envs:
            rules = env.get("protection_rules") or []
            protected = any(r.get("type") == "required_reviewers" and r.get("reviewers") for r in rules)
            found.append(GithubEnvironment(env["name"], protected))
        if len(envs) < 100:
            return found
        page += 1


@dataclass(frozen=True)
class ServerEnvironments:
    proxy: GithubEnvironment | None
    backends: list[GithubEnvironment]   # mc-backend-<Name>: deployed and listed
    listed: list[GithubEnvironment]     # mc-server-<Name>: listed only

    @property
    def listed_in_proxy(self) -> list[GithubEnvironment]:
        return self.backends + self.listed


def classify(envs: list[GithubEnvironment], layout: Layout) -> ServerEnvironments:
    proxy = next((e for e in envs if e.name == layout.proxy_environment), None)
    backends = sorted((e for e in envs if e.name.startswith(layout.backend_prefix)), key=lambda e: e.name)
    listed = sorted((e for e in envs if e.name.startswith(layout.listed_prefix)), key=lambda e: e.name)
    problems: list[str] = []
    names: dict[str, str] = {}
    for env, prefix in [(e, layout.backend_prefix) for e in backends] + [(e, layout.listed_prefix) for e in listed]:
        try:
            name = server_name_for(env.name, prefix)
        except McDeployError as exc:
            problems.append(str(exc))
            continue
        if name.lower() in names:
            problems.append(f"server name '{name}' is used by both {names[name.lower()]} and {env.name}")
        names[name.lower()] = env.name
    fail_if(problems, "GitHub Environments do not form a valid server list:")
    return ServerEnvironments(proxy, backends, listed)


def _fernet(values: dict[str, str]) -> Fernet:
    key = require_values(["MC_COLLECT_KEY"], values, "the server list hand-over (repository secret)")["MC_COLLECT_KEY"]
    try:
        return Fernet(key.encode())
    except ValueError:
        raise McDeployError("MC_COLLECT_KEY is not a valid key; create one with `mc gen-collect-key`") from None


def validate_entry(environment: str, host: str, port: str) -> str:
    """'host:port' for Velocity, after checking both parts."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOST_RE.match(host):
            raise McDeployError(f"{environment}: MC_SERVER_HOST / MC_SERVER_PROXY_HOST is not a valid host name or IPv4 address") from None
    if ":" in host:
        raise McDeployError(f"{environment}: IPv6 hosts are not supported in velocity.toml entries")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise McDeployError(f"{environment}: MC_SERVER_MC_PORT must be a port number")
    return f"{host}:{port}"


def collect_server(environment: str, values: dict[str, str], layout: Layout | None = None) -> Path:
    """Run inside the server's own environment: write its encrypted Velocity entry."""
    layout = layout or load_layout()
    prefix = next((p for p in (layout.backend_prefix, layout.listed_prefix) if environment.startswith(p)), None)
    if prefix is None:
        raise McDeployError(f"{environment} is not a server environment ({layout.backend_prefix}* or {layout.listed_prefix}*)")
    name = server_name_for(environment, prefix)
    v = require_values(["MC_SERVER_HOST", "MC_SERVER_MC_PORT"], values, f"{environment} (environment secrets)")
    host = values.get("MC_SERVER_PROXY_HOST") or v["MC_SERVER_HOST"]
    address = validate_entry(environment, host, v["MC_SERVER_MC_PORT"])
    payload = {"v": 1, "environment": environment, "name": name, "address": address,
               "run": os.environ.get("GITHUB_RUN_ID", "local")}
    out_dir = COLLECT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{environment}{COLLECTED_SUFFIX}"
    path.write_bytes(_fernet(values).encrypt(json.dumps(payload).encode()))
    print(f"{environment}: server entry '{name}' collected (encrypted)")
    return path


def assemble_servers(collected_dir: Path, expected: list[str], values: dict[str, str]) -> str:
    """Decrypt all collected entries and return the JSON object for MC_VELOCITY_SERVERS.
    Fails unless exactly the expected environments delivered an entry from this workflow run."""
    fernet = _fernet(values)
    run = os.environ.get("GITHUB_RUN_ID", "local")
    entries: dict[str, dict] = {}
    problems: list[str] = []
    files = sorted(collected_dir.glob(f"*{COLLECTED_SUFFIX}")) if collected_dir.is_dir() else []
    for path in files:
        try:
            payload = json.loads(fernet.decrypt(path.read_bytes(), ttl=MAX_AGE_SECONDS))
        except InvalidToken:
            problems.append(f"{path.name}: cannot be decrypted (wrong MC_COLLECT_KEY, tampered or older than 24h)")
            continue
        env = payload.get("environment")
        if f"{env}{COLLECTED_SUFFIX}" != path.name:
            problems.append(f"{path.name}: belongs to environment {env!r}")
        elif payload.get("run") != run:
            problems.append(f"{path.name}: comes from workflow run {payload.get('run')}, not this run")
        else:
            entries[env] = payload
    missing = sorted(set(expected) - set(entries))
    extra = sorted(set(entries) - set(expected))
    if missing:
        problems.append(f"no server entry from: {', '.join(missing)}")
    if extra:
        problems.append(f"unexpected server entries from: {', '.join(extra)}")
    fail_if(problems, "Cannot build the Velocity server list:")
    servers = {entries[env]["name"]: entries[env]["address"] for env in sorted(entries)}
    return json.dumps(servers, separators=(",", ":"))


def collected_value(values: dict[str, str]) -> str | None:
    """The assembled server list when the workflow provided collected entries, else None."""
    collected = os.environ.get("MC_COLLECTED_DIR")
    if not collected:
        return None
    expected = [e for e in os.environ.get("MC_EXPECTED_SERVERS", "").split(",") if e]
    if not expected:
        raise McDeployError("MC_COLLECTED_DIR is set but MC_EXPECTED_SERVERS is empty")
    return assemble_servers(Path(collected), expected, values)


def generate_key() -> str:
    return Fernet.generate_key().decode()
