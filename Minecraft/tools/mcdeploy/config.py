"""Loading of the repo-side configuration: targets, placeholder schema, test values, managed files."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

from .errors import McDeployError, fail_if

REPO_ROOT = Path(os.environ.get("MC_REPO_ROOT", Path.cwd())).resolve()
MC_DIR = REPO_ROOT / "Minecraft"
TARGETS_FILE = MC_DIR / "targets.yml"
SCHEMA_FILE = MC_DIR / "secrets.schema.yml"
KNOBS_FILE = MC_DIR / "knobs.yml"
TEST_VALUES_FILE = MC_DIR / "test" / "test-values.env"
LOG_ALLOWLIST_FILE = MC_DIR / "test" / "log-allowlist.yml"
SCAN_ALLOWLIST_FILE = MC_DIR / "secret-scan-allowlist.yml"
OUT_DIR = REPO_ROOT / ".cache" / "out"
CACHE_DIR = REPO_ROOT / ".cache"

NAME_RE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# One server's secrets for local runs; read by the tool (not docker --env-file, whose errors echo lines).
LOCAL_ENV_FILE = REPO_ROOT / ".mc-local.env"
# Keys in test-values.env that configure the test stack itself instead of filling a placeholder.
TEST_STACK_KEYS = {"TEST_MYSQL_ROOT_PASSWORD", "TEST_MYSQL_DATABASES"}
PLATFORMS = {"paper", "velocity"}


def load_yaml(path: Path):
    if not path.is_file():
        raise McDeployError(f"Required file is missing: {path.relative_to(REPO_ROOT)}")
    yaml = YAML(typ="safe")
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh)


SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PROFILE_KEYS = {
    "source": str, "platform": str, "java_image": str, "server_jar": str, "accept_eula": bool,
    "memory": str, "boot_timeout_seconds": int, "settle_seconds": int,
}


@dataclass(frozen=True)
class Target:
    """A deployable server. Its name IS its GitHub Environment name (mc-proxy, mc-backend-<Name>)."""
    name: str
    profile: str          # "proxy" or "backend"
    server_name: str      # name in Velocity's [servers] ("" for the proxy)
    source: str
    platform: str
    java_image: str
    server_jar: str
    accept_eula: bool
    memory: str
    boot_timeout_seconds: int
    settle_seconds: int

    @property
    def environment(self) -> str:
        return self.name

    @property
    def source_dir(self) -> Path:
        return MC_DIR / self.source


@dataclass(frozen=True)
class Layout:
    """targets.yml: how GitHub Environment names map to servers."""
    proxy_environment: str
    backend_prefix: str
    listed_prefix: str
    profiles: dict  # profile name -> dict of PROFILE_KEYS

    def sources(self) -> dict[str, str]:
        """Config source dir -> profile name."""
        return {p["source"]: name for name, p in self.profiles.items()}


def load_layout() -> Layout:
    data = load_yaml(TARGETS_FILE) or {}
    problems: list[str] = []
    profiles: dict[str, dict] = {}
    for name in ("proxy", "backend"):
        spec = data.get(name)
        if not isinstance(spec, dict):
            problems.append(f"'{name}' section is missing")
            continue
        extra_key = "environment" if name == "proxy" else "environment_prefix"
        if not isinstance(spec.get(extra_key), str) or not spec[extra_key]:
            problems.append(f"{name}: '{extra_key}' is required")
        for key, typ in PROFILE_KEYS.items():
            if key not in spec:
                problems.append(f"{name}: missing '{key}'")
            elif not isinstance(spec[key], typ) or (typ is int and isinstance(spec[key], bool)):
                problems.append(f"{name}: '{key}' must be {typ.__name__}")
        unknown = set(spec) - set(PROFILE_KEYS) - {extra_key}
        if unknown:
            problems.append(f"{name}: unknown keys {sorted(unknown)}")
        if spec.get("platform") not in PLATFORMS:
            problems.append(f"{name}: platform must be one of {sorted(PLATFORMS)}")
        if isinstance(spec.get("source"), str) and not (MC_DIR / spec["source"]).is_dir():
            problems.append(f"{name}: source dir Minecraft/{spec['source']} does not exist")
        profiles[name] = spec
    listed = data.get("listed_server_prefix")
    if not isinstance(listed, str) or not listed:
        problems.append("'listed_server_prefix' is required")
    unknown = set(data) - {"proxy", "backend", "listed_server_prefix"}
    if unknown:
        problems.append(f"unknown top-level keys {sorted(unknown)}")
    fail_if(problems, "targets.yml is invalid:")
    return Layout(profiles["proxy"]["environment"], profiles["backend"]["environment_prefix"], listed, profiles)


def server_name_for(environment: str, prefix: str) -> str:
    name = environment[len(prefix):]
    if not SERVER_NAME_RE.match(name):
        raise McDeployError(f"environment '{environment}': server name '{name}' may only contain letters, digits, '_' and '-'")
    return name


def get_target(environment: str, layout: Layout | None = None) -> Target:
    """Target for a GitHub Environment name: the proxy or a mc-backend-<Name> backend."""
    layout = layout or load_layout()
    if environment == layout.proxy_environment:
        profile, server_name = "proxy", ""
    elif environment.startswith(layout.backend_prefix):
        profile, server_name = "backend", server_name_for(environment, layout.backend_prefix)
    else:
        raise McDeployError(
            f"'{environment}' is not deployable: use '{layout.proxy_environment}' or "
            f"'{layout.backend_prefix}<Name>' ({layout.listed_prefix}<Name> servers are only listed in velocity.toml)"
        )
    spec = layout.profiles[profile]
    return Target(name=environment, profile=profile, server_name=server_name,
                  **{k: spec[k] for k in PROFILE_KEYS})


@dataclass(frozen=True)
class SchemaEntry:
    name: str
    kind: str  # "secret" or "var"
    description: str
    sources: tuple[str, ...]  # config folders under Minecraft/ that use it
    mask: str = "everywhere"  # "keyed": value is a common word, never replaced globally when masking
    expand: str = ""          # "toml-entries": JSON object expanded into key = "value" lines or array names (see blocks.py)
    scope: str = "environment"  # repository | environment | collected (built from the server environments)


SCOPES = ("repository", "environment", "collected")


def load_schema(layout: Layout | None = None) -> dict[str, SchemaEntry]:
    """Load the placeholder schema; `sources` are checked against targets.yml."""
    layout = layout or load_layout()
    known_sources = set(layout.sources())
    data = load_yaml(SCHEMA_FILE) or {}
    raw = data.get("placeholders") or {}
    if not isinstance(raw, dict):
        raise McDeployError("secrets.schema.yml: 'placeholders' must be a mapping")
    problems: list[str] = []
    entries: dict[str, SchemaEntry] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append(f"{name!r}: names must match {NAME_RE.pattern}")
            continue
        if not isinstance(spec, dict):
            problems.append(f"{name}: must be a mapping")
            continue
        kind = spec.get("kind")
        desc = spec.get("description")
        sources = spec.get("sources")
        if kind not in ("secret", "var"):
            problems.append(f"{name}: kind must be 'secret' or 'var'")
        if not isinstance(desc, str) or not desc.strip():
            problems.append(f"{name}: description is required")
        if not isinstance(sources, list) or not sources or not all(isinstance(t, str) for t in sources):
            problems.append(f"{name}: sources must be a non-empty list of config folders ({sorted(known_sources)})")
            continue
        for src in sources:
            if src not in known_sources:
                problems.append(f"{name}: unknown source '{src}' (known: {sorted(known_sources)})")
        mask_scope = spec.get("mask", "everywhere")
        if mask_scope not in ("everywhere", "keyed"):
            problems.append(f"{name}: mask must be 'everywhere' or 'keyed'")
        scope = spec.get("scope")
        if scope not in SCOPES:
            problems.append(f"{name}: scope must be one of {SCOPES}")
        unknown = set(spec) - {"kind", "description", "sources", "mask", "expand", "scope"}
        if unknown:
            problems.append(f"{name}: unknown keys {sorted(unknown)}")
        expand = spec.get("expand", "")
        if expand not in ("", "toml-entries"):
            problems.append(f"{name}: expand must be 'toml-entries' (or omitted)")
        entries[name] = SchemaEntry(name, kind, desc or "", tuple(sources), mask_scope, expand, scope or "environment")
    fail_if(problems, "secrets.schema.yml is invalid:")
    return entries


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value lines. Values are taken literally (no quotes, no expansion).
    Errors name the line number only: these files hold secrets."""
    if not path.is_file():
        raise McDeployError(f"Required file is missing: {path}")
    values: dict[str, str] = {}
    problems: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not ENV_KEY_RE.match(key):
            problems.append(f"line {lineno}: expected KEY=value, e.g. MC_SERVER_HOST=... (not KEY: value)")
            continue
        if key in values:
            problems.append(f"line {lineno}: duplicate key {key}")
        values[key] = value
    fail_if(problems, f"{path.name} is invalid (contents not shown):")
    return values


def load_test_values() -> dict[str, str]:
    return parse_env_file(TEST_VALUES_FILE)


def deploy_values() -> dict[str, str]:
    """Secret and variable values for a real deploy, read from the environment.

    In GitHub Actions each job's `env` passes exactly the secrets it needs, one by one (lint
    checks that list against the schema). A secret that is not set arrives as an empty string
    and is rejected by require_values. Locally the values come from .mc-local.env.
    """
    values: dict[str, str] = dict(os.environ)
    if not os.environ.get("CI") and not os.environ.get("GITHUB_ACTIONS") and LOCAL_ENV_FILE.is_file():
        for key, value in parse_env_file(LOCAL_ENV_FILE).items():
            values.setdefault(key, value)  # variables set in the shell win over the file
    # "collected" placeholders (the Velocity server list) never come from a GitHub secret of the
    # same name: only from the entries the server environments delivered in this run.
    collected = [n for n, e in load_schema().items() if e.scope == "collected"]
    for name in collected:
        values.pop(name, None)
    if collected:
        from .servers import collected_value

        assembled = collected_value(values)
        if assembled is not None:
            values.update({name: assembled for name in collected})
    return values


def require_values(names: list[str], values: dict[str, str], context: str) -> dict[str, str]:
    """Return the requested values, failing with the full list of missing or empty ones."""
    missing = [n for n in names if not values.get(n)]
    fail_if(sorted(missing), f"Missing or empty values for {context} (nothing was changed):")
    return {n: values[n] for n in names}


def no_git() -> bool:
    """MC_NO_GIT=1: never invoke git (e.g. while someone curates the index by hand)."""
    return os.environ.get("MC_NO_GIT") == "1"


def _ignore_spec_files(paths: list[str]) -> list[str]:
    """Git-free listing: walk the tree and apply the root .gitignore with git's matching rules.
    Only used with MC_NO_GIT=1; CI always asks git itself."""
    from pathspec import GitIgnoreSpec

    gitignore = REPO_ROOT / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8-sig").splitlines() if gitignore.is_file() else []
    if any(p.name == ".gitignore" for p in MC_DIR.rglob(".gitignore")):
        raise McDeployError("MC_NO_GIT supports only the root .gitignore; nested .gitignore files found")
    spec = GitIgnoreSpec.from_lines(lines)
    files: list[str] = []
    for prefix in paths:
        base = (REPO_ROOT / prefix).resolve()
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and ".git" not in path.relative_to(REPO_ROOT).parts:
                rel = path.relative_to(REPO_ROOT).as_posix()
                if not spec.match_file(rel):
                    files.append(rel)
    return sorted(set(files))


def git_files(paths: list[str], include_untracked: bool = True) -> list[str]:
    """Files git would track under the given repo-relative paths (respecting .gitignore)."""
    if no_git():
        return _ignore_spec_files(paths)
    args = ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached"]
    if include_untracked:
        args += ["--others", "--exclude-standard"]
    result = subprocess.run(args + ["--", *paths], capture_output=True, check=False)
    if result.returncode != 0:
        raise McDeployError(f"git ls-files failed: {result.stderr.decode(errors='replace').strip()}")
    files = sorted({f for f in result.stdout.decode("utf-8").split("\0") if f})
    return [f for f in files if (REPO_ROOT / f).is_file()]


def source_files(source: str, include_untracked: bool = True) -> list[str]:
    """Paths relative to Minecraft/<source> of every tracked (or trackable) file in that folder."""
    prefix = f"Minecraft/{source}/"
    return [f[len(prefix):] for f in git_files([prefix], include_untracked)]


def managed_files(target: Target, include_untracked: bool = True) -> list[str]:
    """Repo-relative paths (inside the target's source dir) that this target deploys."""
    return source_files(target.source, include_untracked)


def assert_clean_worktree(target: Target) -> None:
    if no_git():
        raise McDeployError("deploys need git to prove the files are committed; unset MC_NO_GIT")
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", f"Minecraft/{target.source}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise McDeployError(f"git status failed: {result.stderr.strip()}")
    if result.stdout.strip():
        raise McDeployError(
            f"Minecraft/{target.source} has uncommitted changes; deploys only ship committed files:\n"
            + result.stdout
        )


@dataclass
class Outputs:
    """Collects GitHub step summary / outputs; mc.sh forwards them to the runner files."""
    summary: list[str] = field(default_factory=list)

    def add_summary(self, text: str) -> None:
        self.summary.append(text)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUT_DIR / "summary.md").open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip("\n") + "\n")

    @staticmethod
    def set_output(name: str, value: str) -> None:
        if "\n" in value:
            raise McDeployError(f"Output {name} must be a single line")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUT_DIR / "github_output").open("a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


OUTPUTS = Outputs()
