import json

import pytest

import mcdeploy.servers as servers
from mcdeploy.ci import plan
from mcdeploy.config import Layout, get_target
from mcdeploy.errors import McDeployError
from mcdeploy.servers import GithubEnvironment as Env
from mcdeploy.servers import assemble_servers, classify, collect_server, generate_key

PROFILE = {"source": "Backend", "platform": "paper", "java_image": "x", "server_jar": "*.jar",
           "accept_eula": True, "memory": "1G", "boot_timeout_seconds": 1, "settle_seconds": 0}
LAYOUT = Layout("mc-proxy", "mc-backend-", "mc-server-",
                {"proxy": {**PROFILE, "source": "Proxy", "platform": "velocity", "environment": "mc-proxy"},
                 "backend": {**PROFILE, "environment_prefix": "mc-backend-"}})
ENVS = [Env("mc-proxy", True), Env("mc-backend-Test", False), Env("mc-backend-Prod", True),
        Env("mc-server-Lobby", False), Env("unrelated", False)]


def test_targets_come_from_environment_names():
    t = get_target("mc-backend-Adventure", LAYOUT)
    assert (t.profile, t.server_name, t.source, t.environment) == ("backend", "Adventure", "Backend", "mc-backend-Adventure")
    assert get_target("mc-proxy", LAYOUT).profile == "proxy"
    with pytest.raises(McDeployError, match="only listed"):
        get_target("mc-server-Lobby", LAYOUT)
    with pytest.raises(McDeployError, match="may only contain"):
        get_target("mc-backend-Bad.Name", LAYOUT)


def test_classify_rejects_duplicate_server_names():
    with pytest.raises(McDeployError, match="used by both"):
        classify([Env("mc-backend-Lobby", False), Env("mc-server-lobby", False)], LAYOUT)


def test_push_to_backend_only_deploys_backends_without_collectors():
    s = classify(ENVS, LAYOUT)
    backends, collectors, proxy, expected, _ = plan("push", ["Minecraft/Backend/x.yml"], [], s, LAYOUT)
    assert [b["environment"] for b in backends] == ["mc-backend-Prod", "mc-backend-Test"]
    assert not proxy and collectors == [] and expected == []


def test_proxy_change_collects_every_listed_server():
    s = classify(ENVS, LAYOUT)
    backends, collectors, proxy, expected, _ = plan("push", ["Minecraft/Proxy/velocity.toml"], [], s, LAYOUT)
    assert proxy and backends == []
    assert [c["environment"] for c in collectors] == ["mc-backend-Prod", "mc-backend-Test", "mc-server-Lobby"]
    assert expected == ["mc-backend-Prod", "mc-backend-Test", "mc-server-Lobby"]


def test_backend_jobs_deliver_entries_when_both_change():
    s = classify(ENVS, LAYOUT)
    backends, collectors, proxy, expected, _ = plan("push", None, [], s, LAYOUT)
    assert proxy and all(b["collect"] for b in backends)
    assert [c["environment"] for c in collectors] == ["mc-server-Lobby"]
    assert len(expected) == 3


def test_pull_request_tests_only_unprotected_backends():
    s = classify(ENVS, LAYOUT)
    backends, collectors, proxy, _, _ = plan("pull_request", None, [], s, LAYOUT)
    assert [b["environment"] for b in backends] == ["mc-backend-Test"] and not proxy and collectors == []


def test_dispatch_rejects_unknown_targets():
    with pytest.raises(McDeployError, match="not deployable"):
        plan("workflow_dispatch", None, ["mc-server-Lobby"], classify(ENVS, LAYOUT), LAYOUT)


@pytest.fixture()
def collect_env(tmp_path, monkeypatch):
    monkeypatch.setattr(servers, "COLLECT_OUT_DIR", tmp_path / "collected")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    key = generate_key()
    return tmp_path / "collected", key


def test_collected_entries_round_trip(collect_env):
    out, key = collect_env
    collect_server("mc-backend-Test", {"MC_COLLECT_KEY": key, "MC_SERVER_HOST": "203.0.113.9",
                                       "MC_SERVER_PROXY_HOST": "127.0.0.1", "MC_SERVER_MC_PORT": "25566"}, LAYOUT)
    collect_server("mc-server-Lobby", {"MC_COLLECT_KEY": key, "MC_SERVER_HOST": "127.0.0.1",
                                       "MC_SERVER_MC_PORT": "25565"}, LAYOUT)
    assert "203.0.113.9" not in (out / "mc-backend-Test.server.enc").read_text()
    result = json.loads(assemble_servers(out, ["mc-backend-Test", "mc-server-Lobby"], {"MC_COLLECT_KEY": key}))
    assert result == {"Test": "127.0.0.1:25566", "Lobby": "127.0.0.1:25565"}


def test_missing_entry_fails(collect_env):
    out, key = collect_env
    collect_server("mc-server-Lobby", {"MC_COLLECT_KEY": key, "MC_SERVER_HOST": "h.example", "MC_SERVER_MC_PORT": "1"}, LAYOUT)
    with pytest.raises(McDeployError, match="no server entry from: mc-backend-Test"):
        assemble_servers(out, ["mc-backend-Test", "mc-server-Lobby"], {"MC_COLLECT_KEY": key})


def test_entry_from_other_run_or_key_fails(collect_env, monkeypatch):
    out, key = collect_env
    collect_server("mc-server-Lobby", {"MC_COLLECT_KEY": key, "MC_SERVER_HOST": "h.example", "MC_SERVER_MC_PORT": "1"}, LAYOUT)
    monkeypatch.setenv("GITHUB_RUN_ID", "43")
    with pytest.raises(McDeployError, match="not this run"):
        assemble_servers(out, ["mc-server-Lobby"], {"MC_COLLECT_KEY": key})
    with pytest.raises(McDeployError, match="cannot be decrypted"):
        assemble_servers(out, ["mc-server-Lobby"], {"MC_COLLECT_KEY": generate_key()})


def test_collector_requires_host_and_port(collect_env):
    _, key = collect_env
    with pytest.raises(McDeployError, match="MC_SERVER_MC_PORT"):
        collect_server("mc-server-Lobby", {"MC_COLLECT_KEY": key, "MC_SERVER_HOST": "h.example"}, LAYOUT)
    with pytest.raises(McDeployError, match="port number"):
        collect_server("mc-server-Lobby", {"MC_COLLECT_KEY": key, "MC_SERVER_HOST": "h.example", "MC_SERVER_MC_PORT": "99999"}, LAYOUT)
