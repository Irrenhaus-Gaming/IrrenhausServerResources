import json
import struct

import pytest
from ruamel.yaml import YAML

from mcdeploy.errors import McDeployError
from mcdeploy.play import (
    PAPER_GLOBAL, RSPM_CONFIG, merge_options, offline_uuid, play_overrides, servers_dat,
)

PAPER = (
    "proxies:\n"
    "  bungee-cord:\n"
    "    online-mode: true\n"
    "  velocity:\n"
    "    enabled: true\n"
    "    online-mode: true\n"
    '    secret: "dummy"\n'
)
RSPM = (
    "# Automatically host the resource pack\n"
    "autoHost: true\n"
    "selfHostForce: false\n"
    "selfHostExternalUrl: ''\n"
    "preferSelfHost: true\n"
)


def rendered():
    return {
        PAPER_GLOBAL: PAPER.encode(),
        RSPM_CONFIG: RSPM.encode(),
        "ops.json": json.dumps([{"uuid": "x", "name": "Someone", "level": 4}]).encode(),
    }


def test_offline_uuid_matches_java():
    # Java: UUID.nameUUIDFromBytes("OfflinePlayer:Notch".getBytes(UTF_8))
    assert offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"


def test_overrides_disable_velocity_and_self_host_the_pack():
    out = play_overrides(rendered(), "ClaudeBot")
    paper = YAML().load(out[PAPER_GLOBAL].decode())
    assert paper["proxies"]["velocity"]["enabled"] is False
    assert paper["proxies"]["velocity"]["secret"] == "dummy"
    rspm = YAML().load(out[RSPM_CONFIG].decode())
    assert rspm["autoHost"] is False and rspm["selfHostForce"] is True
    assert rspm["selfHostExternalUrl"] == "http://localhost:25566/rspm.zip"
    assert "# Automatically host the resource pack" in out[RSPM_CONFIG].decode()
    props = out["server.properties"].decode()
    assert "online-mode=false" in props and "server-port=25565" in props
    ops = json.loads(out["ops.json"])
    assert [o["name"] for o in ops] == ["Someone", "ClaudeBot"]
    assert ops[0]["uuid"] == offline_uuid("Someone") and ops[0]["level"] == 4
    assert ops[1]["uuid"] == offline_uuid("ClaudeBot") and ops[1]["level"] == 4


def test_overrides_leave_the_input_untouched():
    src = rendered()
    play_overrides(src, "ClaudeBot")
    assert src[PAPER_GLOBAL] == PAPER.encode()


def test_missing_key_fails_loudly():
    src = rendered()
    src[RSPM_CONFIG] = b"autoHost: true\n"
    with pytest.raises(McDeployError, match="selfHostForce"):
        play_overrides(src, "ClaudeBot")


def test_servers_dat_is_valid_nbt():
    data = servers_dat([("localhost:25565", "Local")])
    assert data[:3] == b"\x0a\x00\x00"                      # root compound, empty name
    assert data[3:4] == b"\x09" and b"servers" in data       # list "servers"
    count_at = data.index(b"servers") + len("servers") + 1   # after the element type byte
    assert struct.unpack(">i", data[count_at:count_at + 4])[0] == 1
    assert b"localhost:25565" in data and b"acceptTextures\x01" in data
    assert data.endswith(b"\x00\x00")                        # end of entry, end of root


def test_published_ports_are_loopback_only():
    from mcdeploy.play import LOCAL_PORTS
    assert LOCAL_PORTS and all(spec.startswith("127.0.0.1:") for spec in LOCAL_PORTS)


def test_merge_options_replaces_and_appends():
    out = merge_options("pauseOnLostFocus:true\nfov:0.0\n", {"pauseOnLostFocus": "false", "maxFps": "30"})
    assert out == "pauseOnLostFocus:false\nfov:0.0\nmaxFps:30\n"
