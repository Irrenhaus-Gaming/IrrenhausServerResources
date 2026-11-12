"""Local play server and a real game client for visual checks (models, holograms, menus).

`mc play start <target>` boots the target like the boot test (rendered configs, dummy test values,
throwaway MySQL) but keeps it running, with a few local-only overrides (see play_overrides): offline
mode without Velocity, a flat creative world, and ResourcePackManager serving the merged pack itself
on the Minecraft port + 1 instead of uploading it to magmaguy.com. Both ports are always published on
127.0.0.1, so the owner can join with their own client (localhost:25565) whenever something is tested.

`mc client start` runs the real Minecraft client (Minecraft/tools/client) on a virtual screen in its
own container. It shares the server's network namespace, so it joins localhost and downloads the pack
from localhost. `mc client …` drives that client with xdotool and
saves screenshots of its virtual screen to .cache/play/shots/.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from ruamel.yaml import YAML

from . import boottest
from .boottest import ANSI_RE, docker, wait_for
from .config import CACHE_DIR, REPO_ROOT, Target
from .deploy import render_target
from .errors import McDeployError

MYSQL_NAME = "mcd-play-mysql"
SERVER_NAME = "mcd-play-server"
CLIENT_NAME = "mcd-play-client"
CLIENT_IMAGE = "irrenhaus-mc-client:local"
CLIENT_VOLUME = "mcd-play-client-data"   # game files (client jar, assets, JVM) survive restarts
CLIENT_CONTEXT = REPO_ROOT / "Minecraft" / "tools" / "client"
SHOT_DIR = CACHE_DIR / "play" / "shots"
RUN_LABEL = "mcdeploy.play.run"         # which `play start` created a container
TARGET_LABEL = "mcdeploy.play.target"
PACK_LIVE = "Resource pack is live via self-hosting"
MC_PORT = 25565
PACK_PORT = MC_PORT + 1   # ResourcePackManager self-host: Minecraft port + networkHttpOffset-v2 (1)
DEFAULT_PLAYER = "ClaudeBot"
DEFAULT_SIZE = "1280x720"
NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
SHOT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
VERSION_RE = re.compile(r"for Minecraft (\S+)")

PAPER_GLOBAL = "config/paper-global.yml"
RSPM_CONFIG = "plugins/ResourcePackManager/config.yml"

# Local play only, never deployed. The repo has no server.properties (AMP owns it).
SERVER_PROPERTIES = f"""\
# mc play: local visual checks only
online-mode=false
enforce-secure-profile=false
server-port={MC_PORT}
motd=Irrenhaus local play
max-players=4
level-type=minecraft\\:flat
generate-structures=false
difficulty=peaceful
gamemode=creative
force-gamemode=true
spawn-protection=0
view-distance=6
simulation-distance=5
white-list=false
"""

# Client options merged into the game's options.txt before every start.
CLIENT_OPTIONS = {
    "onboardAccessibility": "false",
    "skipMultiplayerWarning": "true",
    "joinedFirstServer": "true",
    "tutorialStep": "none",
    "pauseOnLostFocus": "false",
    "narrator": "0",
    "narratorHotkey": "false",
    "realmsNotifications": "false",
    "renderDistance": "6",
    "simulationDistance": "5",
    "maxFps": "30",
    "enableVsync": "false",
    "fullscreen": "false",
    "guiScale": "2",
    "rawMouseInput": "false",
    "soundCategory_master": "0.0",
    "lang": "en_us",
}


# ---------------------------------------------------------------- server overrides (pure)

def offline_uuid(name: str) -> str:
    """UUID the server gives an offline-mode player (Java's UUID.nameUUIDFromBytes)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _set_yaml(rendered: dict[str, bytes], rel: str, changes: dict[tuple[str, ...], object]) -> bytes:
    """Set existing keys of a rendered YAML file; fail if a key is missing (plugin renamed it)."""
    if rel not in rendered:
        raise McDeployError(f"mc play: {rel} is not among the rendered files")
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(rendered[rel].decode("utf-8"))
    for path, value in changes.items():
        node = data
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict) or path[-1] not in node:
            raise McDeployError(f"mc play: {rel} has no key {'.'.join(path)} to override")
        node[path[-1]] = value
    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue().encode("utf-8")


def play_overrides(rendered: dict[str, bytes], player: str) -> dict[str, bytes]:
    """The rendered backend with the local-only changes `mc play` needs."""
    out = dict(rendered)
    out["server.properties"] = SERVER_PROPERTIES.encode("utf-8")
    # No proxy in front: players connect directly, so Velocity forwarding must be off.
    out[PAPER_GLOBAL] = _set_yaml(rendered, PAPER_GLOBAL, {("proxies", "velocity", "enabled"): False})
    # Serve the merged pack from this container; never upload it to magmaguy.com from a local run.
    out[RSPM_CONFIG] = _set_yaml(rendered, RSPM_CONFIG, {
        ("autoHost",): False,
        ("selfHostForce",): True,
        ("selfHostExternalUrl",): f"http://localhost:{PACK_PORT}/rspm.zip",
    })
    # Offline mode gives every player a name-based UUID, so the repo's ops (online UUIDs) would not
    # match: keep them as ops under their offline UUID, plus the client's player.
    ops = json.loads(rendered.get("ops.json", b"[]").decode("utf-8") or "[]")
    ops = [{**o, "uuid": offline_uuid(o["name"])} for o in ops if o.get("name") != player]
    ops.append({"uuid": offline_uuid(player), "name": player, "level": 4, "bypassesPlayerLimit": True})
    out["ops.json"] = (json.dumps(ops, indent=2) + "\n").encode("utf-8")
    return out


# ---------------------------------------------------------------- client files (pure)

def _nbt_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def servers_dat(entries: list[tuple[str, str]]) -> bytes:
    """Uncompressed NBT server list; acceptTextures=1 loads server resource packs without a prompt."""
    body = b"\x09" + _nbt_string("servers") + b"\x0a" + struct.pack(">i", len(entries))
    for address, name in entries:
        body += b"\x08" + _nbt_string("ip") + _nbt_string(address)
        body += b"\x08" + _nbt_string("name") + _nbt_string(name)
        body += b"\x01" + _nbt_string("acceptTextures") + b"\x01"
        body += b"\x00"
    return b"\x0a" + _nbt_string("") + body + b"\x00"


def merge_options(existing: str, wanted: dict[str, str]) -> str:
    lines = existing.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split(":", 1)[0]
        if key in wanted:
            lines[i] = f"{key}:{wanted[key]}"
            seen.add(key)
    lines += [f"{k}:{v}" for k, v in wanted.items() if k not in seen]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- helpers

def _running(name: str) -> bool:
    result = docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _exists(name: str) -> bool:
    return docker("inspect", name, check=False).returncode == 0


def _require_running(name: str, hint: str) -> None:
    if not _running(name):
        raise McDeployError(f"{name} is not running ({hint})")


def _check_player(player: str) -> None:
    if not NAME_RE.match(player):
        raise McDeployError(f"player name '{player}' must be 3-16 letters, digits or _")


# ---------------------------------------------------------------- mc play

# Loopback only: the server is in offline mode, so anyone who can reach it can join under any
# name, including the op player. Never publish on all interfaces.
LOCAL_PORTS = (f"127.0.0.1:{MC_PORT}:{MC_PORT}", f"127.0.0.1:{PACK_PORT}:{PACK_PORT}")


def play_start(target: Target, jars_from: str, player: str) -> None:
    if target.platform != "paper":
        raise McDeployError("mc play runs a backend (mc-backend-<Name>); the proxy is not supported yet")
    _check_player(player)
    _refuse_if_session_exists()
    inputs = boottest.prepare_boot(target, jars_from, purpose="play server")
    rendered = play_overrides(render_target(target, inputs.values), player)
    _refuse_if_session_exists()   # rendering takes a while; another start may have begun meanwhile
    run_id = uuid.uuid4().hex[:12]
    label = ("--label", f"{RUN_LABEL}={run_id}", "--label", f"{TARGET_LABEL}={target.name}")
    workdir = Path(tempfile.mkdtemp(prefix="mcd-play-"))
    try:
        boottest.fill_workdir(target, rendered, inputs.jar_dir, workdir)
        ports = tuple(arg for spec in LOCAL_PORTS for arg in ("-p", spec))
        boottest.start_mysql(MYSQL_NAME, inputs.root_password, inputs.databases, run_args=label + ports)
        boottest.start_server_container(target, SERVER_NAME, MYSQL_NAME, workdir, create_args=label)
    except BaseException:
        # Only remove what this run created: with two starts racing, the loser must not
        # delete the winner's containers (its own `docker run` failed on the name).
        for name in (SERVER_NAME, MYSQL_NAME):
            if _label(name, RUN_LABEL) == run_id:
                docker("rm", "-f", name, check=False)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"{target.name}: play server starting (max {target.boot_timeout_seconds}s)")
    boottest.wait_until_started(target, SERVER_NAME)
    print(f"{target.name}: play server is up; offline mode, {player} is op. Next: mc client start")
    print(f"join with your own client: Multiplayer -> Direct Connection -> localhost:{MC_PORT}, "
          f"Minecraft {server_minecraft_version()} (this PC only; everyone in ops.json is admin, "
          f"others: mc play console \"op <name>\")")


def _label(name: str, key: str) -> str | None:
    result = docker("inspect", "-f", f'{{{{index .Config.Labels "{key}"}}}}', name, check=False)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value if value and value != "<no value>" else None


def _refuse_if_session_exists() -> None:
    existing = [n for n in (CLIENT_NAME, SERVER_NAME, MYSQL_NAME) if _exists(n)]
    if existing:
        raise McDeployError(
            f"a play session exists already ({', '.join(existing)}). Check it with `mc play status`: "
            f"reuse it if it fits, and run `mc play stop` only if it is yours or the owner agreed "
            f"(someone may be playing on it)")


def play_status() -> None:
    """What exists, whether the server is ready and who is online; never changes anything."""
    for name in (MYSQL_NAME, SERVER_NAME, CLIENT_NAME):
        state = docker("inspect", "-f", "{{.State.Status}} since {{.State.StartedAt}}", name, check=False)
        print(f"{name}: {state.stdout.strip() if state.returncode == 0 else 'absent'}")
    if not _exists(MYSQL_NAME) and not _exists(SERVER_NAME):
        print("no play session: start one with `mc play start <target>`")
        return
    print(f"target: {_label(SERVER_NAME, TARGET_LABEL) or _label(MYSQL_NAME, TARGET_LABEL) or 'unknown'}")
    ports = docker("port", MYSQL_NAME, check=False).stdout.strip().replace("\n", ", ")
    print(f"published ports: {ports or 'none'}")
    if not _running(SERVER_NAME):
        print("server: not running (a start may be in progress, or it failed: mc play log)")
        return
    logs = ANSI_RE.sub("", docker("logs", SERVER_NAME, check=False).stdout)
    ready = bool(boottest.DONE_RE.search(logs))
    print(f"server: {'up' if ready else 'starting'}"
          + (f", Minecraft {VERSION_RE.search(logs).group(1)}" if VERSION_RE.search(logs) else ""))
    print(f"resource pack: {'served on localhost:' + str(PACK_PORT) if PACK_LIVE in logs else 'not ready yet'}")
    if ready:
        before = len(logs.splitlines())
        boottest.console_command(SERVER_NAME, "list")
        time.sleep(1.5)
        new = ANSI_RE.sub("", docker("logs", SERVER_NAME, check=False).stdout).splitlines()[before:]
        online = [line.split("]: ", 1)[-1].removeprefix("System chat: ")
                  for line in new if "players online" in line or "out of a maximum" in line]
        print(f"players: {online[0] if online else 'unknown (see mc play log)'}")
        print(f"join: Multiplayer -> Direct Connection -> localhost:{MC_PORT}")


def play_stop() -> None:
    for name in (CLIENT_NAME, SERVER_NAME, MYSQL_NAME):
        if _exists(name):
            docker("rm", "-f", name)
            print(f"removed {name}")
    print(f"play session stopped (game files stay in the Docker volume {CLIENT_VOLUME})")


def play_console(command: str) -> None:
    _require_running(SERVER_NAME, "start it with `mc play start <target>`")
    before = len(docker("logs", SERVER_NAME, check=False).stdout.splitlines())
    boottest.console_command(SERVER_NAME, command.lstrip("/"))
    time.sleep(2)
    lines = ANSI_RE.sub("", docker("logs", SERVER_NAME, check=False).stdout).splitlines()
    print("\n".join(lines[before:]) or "(no console output yet; see mc play log)")


def play_log(tail: int, grep: str | None) -> None:
    if not _exists(SERVER_NAME):
        raise McDeployError(f"{SERVER_NAME} does not exist (start it with `mc play start <target>`)")
    logs = docker("logs", SERVER_NAME, check=False)
    lines = ANSI_RE.sub("", logs.stdout + logs.stderr).splitlines()
    if grep:
        pattern = re.compile(grep, re.I)
        lines = [line for line in lines if pattern.search(line)]
    print("\n".join(lines[-tail:]))


def server_minecraft_version() -> str:
    logs = ANSI_RE.sub("", docker("logs", SERVER_NAME, check=False).stdout)
    match = VERSION_RE.search(logs)
    if match is None:
        raise McDeployError("cannot read the Minecraft version from the play server log")
    return match.group(1)


# ---------------------------------------------------------------- mc client

def _xdo(*args: str) -> None:
    docker("exec", CLIENT_NAME, "xdotool", *args)


def _client_log() -> str:
    result = docker("exec", CLIENT_NAME, "cat", "/data/logs/latest.log", check=False)
    return result.stdout if result.returncode == 0 else ""


def client_start(player: str, size: str, timeout: int) -> None:
    _check_player(player)
    if not re.match(r"^\d{3,4}x\d{3,4}$", size):
        raise McDeployError(f"size '{size}' must look like 1280x720")
    _require_running(SERVER_NAME, "start the server first with `mc play start <target>`")
    if _exists(CLIENT_NAME):
        raise McDeployError(f"{CLIENT_NAME} exists already; run `mc client stop` first")
    version = server_minecraft_version()
    print(f"building {CLIENT_IMAGE}")
    docker("build", "--quiet", "--tag", CLIENT_IMAGE, str(CLIENT_CONTEXT), timeout=1200)
    docker("volume", "create", CLIENT_VOLUME)
    docker("create", "--name", CLIENT_NAME, "--network", f"container:{MYSQL_NAME}",
           "--volume", f"{CLIENT_VOLUME}:/data", CLIENT_IMAGE,
           version, player, "localhost", str(MC_PORT), size)
    try:
        with tempfile.TemporaryDirectory(prefix="mcd-client-") as tmp:
            options = Path(tmp) / "options.txt"
            got = docker("cp", f"{CLIENT_NAME}:/data/options.txt", str(options), check=False)
            existing = options.read_text(encoding="utf-8") if got.returncode == 0 else ""
            options.write_text(merge_options(existing, CLIENT_OPTIONS), encoding="utf-8")
            address = f"localhost:{MC_PORT}"
            (Path(tmp) / "servers.dat").write_bytes(
                servers_dat([(address, "Irrenhaus local"), ("localhost", "Irrenhaus local (no port)")]))
            docker("cp", str(options), f"{CLIENT_NAME}:/data/options.txt")
            docker("cp", str(Path(tmp) / "servers.dat"), f"{CLIENT_NAME}:/data/servers.dat")
        docker("start", CLIENT_NAME)
    except BaseException:
        docker("rm", "-f", CLIENT_NAME, check=False)
        raise
    print(f"client {version} starting as {player} (first start downloads the game, max {timeout}s)")
    joined = re.compile(rf"\b{re.escape(player)} joined the game")

    def in_game():
        if not _running(CLIENT_NAME):
            tail = docker("logs", "--tail", "60", CLIENT_NAME, check=False)
            raise McDeployError(f"the client exited:\n{(tail.stdout + tail.stderr)[-4000:]}")
        return joined.search(ANSI_RE.sub("", docker("logs", SERVER_NAME, check=False).stdout))

    wait_for(in_game, timeout, f"{player} to join the play server", poll=5)
    width, height = size.split("x")
    _xdo("mousemove", str(int(width) // 2), str(int(height) // 2))
    print(f"{player} joined. Resource pack status: mc client log --grep 'pack'; screenshot: mc client shot")


def client_stop() -> None:
    if _exists(CLIENT_NAME):
        docker("rm", "-f", CLIENT_NAME)
    print(f"client stopped (game files stay in the Docker volume {CLIENT_VOLUME})")


def client_cmd(text: str) -> None:
    """Type a chat line or command into the game (opens chat, types, presses Enter)."""
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    if text.startswith("/"):
        opener, rest = "slash", text[1:]
    else:
        opener, rest = "t", text
    # Text and key are arguments of sh, never part of the script. Needs the plain game view
    # (no menu open): close menus with `mc client key Escape` first.
    script = ('set -e; xdotool key "$1"; sleep 0.4; '
              'xdotool type --delay 15 -- "$2"; sleep 0.2; xdotool key Return')
    docker("exec", CLIENT_NAME, "sh", "-c", script, "sh", opener, rest)
    time.sleep(1)
    client_chat(5)


def client_key(keys: list[str]) -> None:
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    for key in keys:
        _xdo("key", key)
        time.sleep(0.2)


def client_type(text: str) -> None:
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    _xdo("type", "--delay", "15", "--", text)


def client_look(dx: int, dy: int) -> None:
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    _xdo("mousemove_relative", "--", str(dx), str(dy))


def client_click(button: str, at: list[int] | None) -> None:
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    if at:
        _xdo("mousemove", str(at[0]), str(at[1]))
        time.sleep(0.2)
    _xdo("click", {"left": "1", "middle": "2", "right": "3"}[button])


def client_shot(name: str | None, no_hud: bool) -> Path:
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    name = name or time.strftime("shot-%Y%m%d-%H%M%S")
    if not SHOT_NAME_RE.match(name):
        raise McDeployError(f"screenshot name '{name}' may only use letters, digits, '.', '_' and '-'")
    if no_hud:
        _xdo("key", "F1")
        time.sleep(0.5)
    try:
        docker("exec", CLIENT_NAME, "import", "-window", "root", "/tmp/shot.png")
    finally:
        if no_hud:
            _xdo("key", "F1")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SHOT_DIR / f"{name}.png"
    docker("cp", f"{CLIENT_NAME}:/tmp/shot.png", str(dest))
    rel = dest.relative_to(REPO_ROOT).as_posix()
    print(f"screenshot: {rel}")
    return dest


def client_chat(lines: int) -> None:
    _require_running(CLIENT_NAME, "start it with `mc client start`")
    chat = [line for line in _client_log().splitlines() if "[CHAT]" in line]
    print("\n".join(chat[-lines:]) or "(no chat lines yet)")


def client_log(tail: int, grep: str | None) -> None:
    if not _exists(CLIENT_NAME):
        raise McDeployError(f"{CLIENT_NAME} does not exist (start it with `mc client start`)")
    lines = _client_log().splitlines()
    if not lines:
        logs = docker("logs", CLIENT_NAME, check=False)
        lines = (logs.stdout + logs.stderr).splitlines()
    if grep:
        pattern = re.compile(grep, re.I)
        lines = [line for line in lines if pattern.search(line)]
    print("\n".join(line[:400] for line in lines[-tail:]))
