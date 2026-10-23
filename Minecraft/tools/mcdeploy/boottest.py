"""Boot test: start the real server jar with the real plugins and the repo configs (rendered with
dummy test values) in Docker, next to a throwaway MySQL, and fail on anything that went wrong.

Containers get files via `docker cp` instead of bind mounts, so this works the same from a local
shell, from act (Docker-in-Docker via the host socket) and on GitHub-hosted runners.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import (
    CACHE_DIR, LOG_ALLOWLIST_FILE, OUTPUTS, Target, load_test_values, load_yaml,
    require_values,
)
from .deploy import render_target, target_placeholders
from .errors import McDeployError, fail_if
from .remote import RemoteBase, SftpRemote
from .config import deploy_values

MYSQL_IMAGE = "mysql:8.4.13"
# Outside .cache/out (which every mc.sh call empties) so the workflow can still upload it.
BOOT_LOG_DIR = CACHE_DIR / "boot-logs"
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HEADER_RE = re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<level>[A-Z]+)\]:?\s?(?P<msg>.*)$")
DONE_RE = re.compile(r"\bDone \(\d+[.,]\d+s\)!")
ERROR_LEVELS = {"ERROR", "SEVERE", "FATAL"}
STACK_RE = re.compile(r"^\s+at [\w$.<>]+\(|^Caused by: |^[\w$.]+(?:Exception|Error)(?::|$)", re.M)
STOP_COMMAND = {"paper": "stop", "velocity": "shutdown"}


# ---------------------------------------------------------------- jars

def jar_cache_dir(target: Target) -> Path:
    return CACHE_DIR / "jars" / target.name


def fetch_jars(target: Target, remote: RemoteBase) -> None:
    """Mirror the server jar and plugins/*.jar from the target into the local cache."""
    root_files = remote.list_files("")
    server_jars = [f for f in root_files if fnmatch.fnmatch(f, target.server_jar)]
    if len(server_jars) != 1:
        raise McDeployError(
            f"{target.name}: server_jar pattern '{target.server_jar}' must match exactly one file in the "
            f"instance root, matched {server_jars or 'nothing'}"
        )
    plugin_jars = [f for f in remote.list_files("plugins") if f.endswith(".jar")]
    if not plugin_jars:
        raise McDeployError(f"{target.name}: no plugin jars found in plugins/ on the server")
    dest = jar_cache_dir(target)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "plugins").mkdir(parents=True)
    wanted = {"server.jar": server_jars[0], **{f"plugins/{j}": f"plugins/{j}" for j in plugin_jars}}
    for local, rel in wanted.items():
        data = remote.read(rel)
        if data is None:
            raise McDeployError(f"{target.name}: {rel} disappeared while downloading")
        (dest / local).write_bytes(data)
    (dest / "source.json").write_text(json.dumps({"server_jar": server_jars[0], "plugins": plugin_jars}, indent=2))
    print(f"{target.name}: fetched {server_jars[0]} and {len(plugin_jars)} plugin jars")


def plugin_names(jar: Path, platform: str) -> list[str]:
    try:
        with zipfile.ZipFile(jar) as zf:
            names = set(zf.namelist())
            if platform == "velocity":
                if "velocity-plugin.json" not in names:
                    return []  # library jar, not a Velocity plugin
                return [json.loads(zf.read("velocity-plugin.json"))["id"]]
            for descriptor in ("paper-plugin.yml", "plugin.yml"):
                if descriptor in names:
                    m = re.search(r"^name:\s*['\"]?([^'\"\r\n]+?)['\"]?\s*$", zf.read(descriptor).decode("utf-8", "replace"), re.M)
                    if not m:
                        raise McDeployError(f"{jar.name}: {descriptor} has no name")
                    return [m.group(1)]
            return []
    except zipfile.BadZipFile:
        raise McDeployError(f"{jar.name}: not a valid jar") from None


# ---------------------------------------------------------------- log analysis

@dataclass
class LogEntry:
    level: str
    text: str  # header message plus continuation lines

    @property
    def is_error(self) -> bool:
        return self.level in ERROR_LEVELS or bool(STACK_RE.search(self.text))


def parse_log(raw: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for line in ANSI_RE.sub("", raw).splitlines():
        m = HEADER_RE.match(line)
        if m:
            entries.append(LogEntry(m.group("level"), m.group("msg")))
        elif entries:
            entries[-1].text += "\n" + line
        elif line.strip():
            entries.append(LogEntry("INFO", line))
    return entries


@dataclass(frozen=True)
class AllowRule:
    targets: tuple[str, ...]
    pattern: re.Pattern
    reason: str


def load_log_allowlist(profile: str) -> list[AllowRule]:
    data = load_yaml(LOG_ALLOWLIST_FILE) or {}
    rules = []
    problems = []
    for idx, item in enumerate(data.get("allow") or []):
        if not isinstance(item, dict):
            problems.append(f"entry {idx}: must be a mapping")
            continue
        targets = item.get("profiles")
        pattern = item.get("pattern")
        reason = item.get("reason")
        if not isinstance(targets, list) or not targets or not all(t in ("backend", "proxy") for t in targets):
            problems.append(f"entry {idx}: 'profiles' must be a non-empty list of backend / proxy")
        if not isinstance(pattern, str) or not pattern:
            problems.append(f"entry {idx}: 'pattern' is required")
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            problems.append(f"entry {idx}: 'reason' must explain why this error is harmless")
        if problems:
            continue
        try:
            compiled = re.compile(pattern, re.S)
        except re.error as exc:
            problems.append(f"entry {idx}: invalid regex: {exc}")
            continue
        if profile in targets:
            rules.append(AllowRule(tuple(targets), compiled, reason))
    fail_if(problems, f"{LOG_ALLOWLIST_FILE.name} is invalid:")
    return rules


def analyse_log(raw: str, target: Target, expected_plugins: list[str]) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) for a finished server log."""
    entries = parse_log(raw)
    rules = load_log_allowlist(target.profile)
    failures: list[str] = []
    warnings: list[str] = []
    for entry in entries:
        if entry.is_error:
            if not any(rule.pattern.search(entry.text) for rule in rules):
                failures.append(f"[{entry.level}] {entry.text[:1500]}")
        elif entry.level in ("WARN", "WARNING"):
            warnings.append(entry.text.splitlines()[0][:300])
    text = ANSI_RE.sub("", raw)
    if not DONE_RE.search(text):
        failures.append("server never reported 'Done (...)!'")
    for name in expected_plugins:
        if target.platform == "paper":
            ok = re.search(rf"Enabling {re.escape(name)} v", text)
        else:
            ok = re.search(rf"Loaded plugin {re.escape(name)}\b", text)
        if not ok:
            failures.append(f"plugin '{name}' was not enabled/loaded")
    return failures, warnings


# ---------------------------------------------------------------- docker

def docker(*args: str, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise McDeployError(f"docker {args[0]} timed out after {timeout}s") from None
    except FileNotFoundError:
        raise McDeployError("docker CLI not found (run through mc.ps1 / mc.sh)") from None
    if check and result.returncode != 0:
        raise McDeployError(f"docker {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def wait_for(predicate, timeout: int, what: str, poll: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(poll)
    raise McDeployError(f"timed out after {timeout}s waiting for {what}")


def start_mysql(name: str, root_password: str, databases: list[str]) -> None:
    docker("run", "-d", "--name", name, "-e", f"MYSQL_ROOT_PASSWORD={root_password}", MYSQL_IMAGE, timeout=600)

    def ready():
        logs = docker("logs", name, check=False)
        combined = logs.stdout + logs.stderr
        if docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() != "true":
            raise McDeployError(f"MySQL container exited during startup:\n{combined[-3000:]}")
        return re.search(r"ready for connections.*port: 3306", combined)

    wait_for(ready, 180, "MySQL to accept connections")
    sql = " ".join(f"CREATE DATABASE IF NOT EXISTS `{db}`;" for db in databases)
    if sql:
        docker("exec", "-e", f"MYSQL_PWD={root_password}", name, "mysql", "-uroot", "-e", sql)


def _list_values_from_real_names(names: dict[str, str]) -> dict[str, str]:
    """For list placeholders (e.g. the Velocity server list) test with the REAL entry names when
    they are available, but dummy addresses: a server referenced in `try` that is missing from the
    real list must fail the test, while no real address is ever used."""
    from .blocks import parse_entries
    from .deploy import load_expansions

    real = deploy_values()
    result: dict[str, str] = {}
    for name in load_expansions():
        if name in names and real.get(name):
            entries = parse_entries(name, real[name])
            result[name] = json.dumps({key: f"127.0.0.1:{30000 + i}" for i, (key, _) in enumerate(entries)})
            print(f"{name}: boot test uses the {len(entries)} real entry names with dummy addresses")
    return result


def run_boot_test(target: Target, jars_from: str) -> None:
    if target.platform == "paper" and not target.accept_eula:
        raise McDeployError(
            f"{target.name}: accept_eula is false in targets.yml. The boot test runs a Minecraft server and "
            f"needs the Mojang EULA (https://aka.ms/MinecraftEULA) accepted; set it to true to confirm."
        )
    test_values = load_test_values()
    names = target_placeholders(target)
    values = require_values(sorted(names), test_values, f"{target.name} boot test (test-values.env)")
    values.update(_list_values_from_real_names(names))
    stack = require_values(["TEST_MYSQL_ROOT_PASSWORD", "TEST_MYSQL_DATABASES"], test_values, "the test MySQL")
    databases = [d.strip() for d in stack["TEST_MYSQL_DATABASES"].split(",") if d.strip()]
    bad = [d for d in databases if not DB_NAME_RE.match(d)]
    fail_if(bad, "TEST_MYSQL_DATABASES contains invalid names:")

    if jars_from == "remote":
        remote = SftpRemote(deploy_values())
        try:
            fetch_jars(target, remote)
        finally:
            remote.close()
    jar_dir = jar_cache_dir(target)
    if not (jar_dir / "server.jar").is_file():
        raise McDeployError(f"{target.name}: no cached jars in {jar_dir}; run with --jars-from remote first")

    expected: list[str] = []
    for jar in sorted((jar_dir / "plugins").glob("*.jar")):
        expected += plugin_names(jar, target.platform)

    rendered = render_target(target, values)
    run_id = os.environ.get("GITHUB_RUN_ID", "") + uuid.uuid4().hex[:8]
    mysql_name = f"mcd-mysql-{target.name}-{run_id}"
    server_name = f"mcd-server-{target.name}-{run_id}"
    workdir = Path(tempfile.mkdtemp(prefix="mcd-boot-"))
    raw_log = ""
    try:
        for rel, data in rendered.items():
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        shutil.copy2(jar_dir / "server.jar", workdir / "server.jar")
        (workdir / "plugins").mkdir(exist_ok=True)
        for jar in (jar_dir / "plugins").glob("*.jar"):
            shutil.copy2(jar, workdir / "plugins" / jar.name)
        (workdir / "eula.txt").write_text("eula=true\n")
        if target.platform == "velocity":
            # Server-only secret in production; a throwaway one for the test.
            (workdir / "forwarding.secret").write_text(secrets.token_urlsafe(24))

        start_mysql(mysql_name, stack["TEST_MYSQL_ROOT_PASSWORD"], databases)
        # Share MySQL's network namespace so "localhost:3306" in configs reaches the test database.
        script = (
            "mkfifo /tmp/console && (sleep infinity > /tmp/console &) && "
            f"exec java -Xms512M -Xmx{target.memory} -jar server.jar"
            + (" --nogui" if target.platform == "paper" else "") + " < /tmp/console"
        )
        docker("create", "--name", server_name, "--network", f"container:{mysql_name}", "-w", "/server",
               target.java_image, "sh", "-c", script, timeout=600)
        docker("cp", f"{workdir}/.", f"{server_name}:/server", timeout=600)
        docker("start", server_name)
        print(f"{target.name}: server container started, waiting for startup (max {target.boot_timeout_seconds}s)")

        def started():
            nonlocal raw_log
            logs = docker("logs", server_name, check=False)
            raw_log = logs.stdout + logs.stderr
            if DONE_RE.search(ANSI_RE.sub("", raw_log)):
                return True
            if docker("inspect", "-f", "{{.State.Running}}", server_name).stdout.strip() != "true":
                raise McDeployError(f"{target.name}: server exited before finishing startup:\n{raw_log[-5000:]}")
            return False

        wait_for(started, target.boot_timeout_seconds, f"{target.name} to finish starting", poll=5)
        print(f"{target.name}: startup finished, letting it settle for {target.settle_seconds}s")
        time.sleep(target.settle_seconds)
        docker("exec", server_name, "sh", "-c", f"echo {STOP_COMMAND[target.platform]} > /tmp/console")
        exit_code = docker("wait", server_name, timeout=240).stdout.strip()
        logs = docker("logs", server_name, check=False)
        raw_log = logs.stdout + logs.stderr
        failures, warnings = analyse_log(raw_log, target, expected)
        if exit_code != "0":
            failures.append(f"server exited with code {exit_code} after the stop command")
    finally:
        BOOT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        (BOOT_LOG_DIR / f"{target.name}.log").write_text(ANSI_RE.sub("", raw_log), encoding="utf-8")
        docker("rm", "-f", server_name, check=False)
        docker("rm", "-f", mysql_name, check=False)
        shutil.rmtree(workdir, ignore_errors=True)

    OUTPUTS.add_summary(
        f"### {target.name}: boot test {'FAILED' if failures else 'passed'}\n"
        f"{len(expected)} plugins expected, {len(failures)} failure(s), {len(warnings)} warning(s).\n"
        + ("\n<details><summary>Warnings (not failing)</summary>\n\n" + "\n".join(f"- `{w}`" for w in warnings) + "\n</details>\n" if warnings else "")
    )
    fail_if(failures, f"{target.name}: boot test failed (full log: .cache/boot-logs/{target.name}.log):")
    print(f"{target.name}: boot test passed ({len(expected)} plugins, {len(warnings)} warnings)")
