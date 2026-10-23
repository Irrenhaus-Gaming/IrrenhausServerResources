"""Boot test: start the real server jar with the real plugins and the repo configs (rendered with
dummy test values) in Docker, next to a throwaway MySQL, and fail on anything that went wrong.

Containers get files via `docker cp` instead of bind mounts, so this works the same from a local
shell, from act (Docker-in-Docker via the host socket) and on GitHub-hosted runners.
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
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
# Paper logs every stack-trace line with its own "[time WARN]:" header; these belong to the entry above.
CONTINUATION_RE = re.compile(r"^\s+at |^\s*\.\.\. \d+ more|^Caused by: |^\s+Suppressed: ")
STACK_RE = re.compile(r"^\s+at [\w$.<>]+\(|^Caused by: |^[\w$.]+(?:Exception|Error)(?::|$)", re.M)
STOP_COMMAND = {"paper": "stop", "velocity": "shutdown"}


# ---------------------------------------------------------------- jars

def jar_cache_dir(target: Target) -> Path:
    return CACHE_DIR / "jars" / target.name


VERSION_FILE = "version_history.json"  # written by some Paper setups into the server folder
SERVER_LOG = "logs/latest.log"          # the running server's console log
VERSION_CACHE = "server_version.txt"    # what the boot test compares against
LOG_VERSION_RE = re.compile(rb"(?:This server is running Paper version|Loading Paper) (\S+)")


def _manifest_version(remote: RemoteBase, rel: str) -> str | None:
    """Paper build from a server jar's MANIFEST.MF, read by random access (not the whole jar)."""
    fh = remote.open_binary(rel)
    if fh is None:
        return None
    try:
        with zipfile.ZipFile(fh) as zf:
            manifest = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile):
        return None
    finally:
        fh.close()
    fields = dict(re.findall(r"^([\w-]+): *(.+?)\s*$", manifest, re.M))
    version = fields.get("Implementation-Version")
    commit = fields.get("Git-Commit")
    if version and commit and version_commit(version) is None:
        version = f"{version}@{commit[:7]}"
    return version or None


def running_paper_version(remote: RemoteBase) -> str:
    """The Paper build the live server runs. Sources, in order: version_history.json, the
    'This server is running Paper version' line of logs/latest.log, and the manifest of the
    server jar Paper last unpacked into versions/<mc>/. Only the version string is kept."""
    notes: list[str] = []
    raw = remote.read(VERSION_FILE)
    if raw is not None:
        return expected_paper_version(raw)
    notes.append(f"{VERSION_FILE}: not found")
    log = remote.read(SERVER_LOG)
    match = LOG_VERSION_RE.search(log) if log is not None else None
    if match is not None:
        return match.group(1).decode("utf-8", "replace")
    notes.append(f"{SERVER_LOG}: " + ("not found" if log is None else f"{len(log)} bytes, no Paper version line"))
    if "versions" in remote.list_dirs(""):
        jars = []
        for sub_dir in remote.list_dirs("versions"):
            for name, (_, mtime) in remote.file_stats(f"versions/{sub_dir}").items():
                if name.endswith(".jar"):
                    jars.append((mtime, f"versions/{sub_dir}/{name}"))
        for _, rel in sorted(jars, reverse=True):  # newest first: the one Paper last started
            version = _manifest_version(remote, rel)
            if version:
                return re.sub(r"\s*\(MC: [^)]*\)\s*$", "", version).strip()
            notes.append(f"{rel}: no version in its manifest")
        if not jars:
            notes.append("versions/: no jars")
    else:
        notes.append("versions/: not found")
    raise McDeployError("cannot tell which Paper build the server runs:\n" + "\n".join(f"  - {n}" for n in notes))


COMMIT_RE = re.compile(r"(?:@|-)g?([0-9a-f]{7,40})(?![0-9a-z])")


def version_commit(version: str) -> str | None:
    """Git commit inside a Paper version string ('26.3-214-main@e65c076' -> 'e65c076')."""
    found = COMMIT_RE.findall(version)
    return found[-1] if found else None


def same_build(expected: str, running: str) -> bool:
    a, b = version_commit(expected), version_commit(running)
    if a and b:
        return a.startswith(b) or b.startswith(a)
    return expected == running


def fetch_jars(target: Target, remote: RemoteBase) -> None:
    """Mirror the server jar and plugins/*.jar from the target into the local cache.

    Jars whose name, size and modification time are unchanged since the last fetch are reused
    (the workflow keeps .cache/jars between runs), so normally only updated jars are downloaded."""
    jar_dir, pattern = posixpath.split(target.server_jar)
    root_stats = remote.file_stats(jar_dir)
    server_jars = sorted(f for f in root_stats if fnmatch.fnmatch(f, pattern))
    if len(server_jars) != 1:
        raise McDeployError(
            f"{target.name}: server_jar '{target.server_jar}' must match exactly one file, matched "
            f"{server_jars or 'nothing'} (run `mc sftp-probe --target {target.name}` to see the candidates)"
        )
    server_rel = posixpath.join(jar_dir, server_jars[0])
    plugin_stats = {f: s for f, s in remote.file_stats("plugins").items() if f.endswith(".jar")}
    if not plugin_stats:
        raise McDeployError(f"{target.name}: no plugin jars found in plugins/ on the server")

    wanted = {"server.jar": (server_rel, root_stats[server_jars[0]]),
              **{f"plugins/{j}": (f"plugins/{j}", s) for j, s in plugin_stats.items()}}
    dest = jar_cache_dir(target)
    manifest_path = dest / "source.json"
    try:
        old = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    except ValueError:
        old = {}
    (dest / "plugins").mkdir(parents=True, exist_ok=True)
    for stale in (dest / "plugins").glob("*.jar"):
        if f"plugins/{stale.name}" not in wanted:
            stale.unlink()
    downloaded = reused = 0
    manifest: dict[str, list] = {}
    for local, (rel, (size, mtime)) in wanted.items():
        path = dest / local
        entry = [rel, size, mtime]
        if old.get(local) == entry and path.is_file() and path.stat().st_size == size:
            reused += 1
        else:
            data = remote.read(rel)
            if data is None:
                raise McDeployError(f"{target.name}: {rel} disappeared while downloading")
            if len(data) != size:
                raise McDeployError(f"{target.name}: {rel} changed while downloading; run again")
            path.write_bytes(data)
            downloaded += 1
        manifest[local] = entry
    manifest_path.write_text(json.dumps(manifest, indent=2))
    version = running_paper_version(remote) if target.platform == "paper" else ""
    (dest / VERSION_CACHE).write_text(version, encoding="utf-8")
    print(f"{target.name}: server jar {server_rel}, {len(plugin_stats)} plugin jars "
          f"({downloaded} downloaded, {reused} unchanged from cache)"
          + (f", server runs Paper {version}" if version else ""))


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
        if m and entries and CONTINUATION_RE.match(m.group("msg")):
            entries[-1].text += "\n" + m.group("msg")
        elif m:
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


def expected_paper_version(version_file: bytes) -> str:
    """The running version from Paper's version_history.json, e.g. '26.3-214-main@e65c076'."""
    try:
        current = json.loads(version_file)["currentVersion"]
    except (ValueError, KeyError, TypeError):
        raise McDeployError(f"{VERSION_FILE} from the server has no readable currentVersion") from None
    return re.sub(r"\s*\(MC: [^)]*\)\s*$", "", current).strip()


def analyse_log(raw: str, target: Target, expected_plugins: list[str],
                expected_version: str | None = None) -> tuple[list[str], list[str]]:
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
    if expected_version is not None:
        running = re.search(r"(?:Paper version|Loading Paper) (\S+)", text)
        if running is None:
            failures.append("the test log shows no Paper version, cannot compare it with the server")
        elif not same_build(expected_version, running.group(1)):
            failures.append(
                f"the test ran Paper {running.group(1)} but the server runs {expected_version}: "
                f"server_jar in targets.yml points at the wrong jar"
            )
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
        version_file = jar_dir / VERSION_CACHE
        expected_version = (version_file.read_text(encoding="utf-8").strip() or None
                            if target.platform == "paper" and version_file.is_file() else None)
        if target.platform == "paper" and expected_version is None:
            raise McDeployError(f"{target.name}: server version unknown; run with --jars-from remote")
        failures, warnings = analyse_log(raw_log, target, expected, expected_version)
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
