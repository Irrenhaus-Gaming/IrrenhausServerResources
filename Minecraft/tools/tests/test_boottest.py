import json
import os

import pytest

import mcdeploy.boottest as boottest
from mcdeploy.boottest import analyse_log, expected_paper_version, fetch_jars, parse_log
from mcdeploy.config import Target
from mcdeploy.errors import McDeployError
from mcdeploy.remote import LocalRemote

TARGET = Target(
    name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
    java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
)
VERSION = json.dumps({"currentVersion": "26.3-214-main@e65c076 (MC: 26.3)"})


class CountingRemote(LocalRemote):
    def __init__(self, root):
        super().__init__(root)
        self.reads = []

    def read(self, rel):
        self.reads.append(rel)
        return super().read(rel)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    srv = tmp_path / "server"
    (srv / "plugins").mkdir(parents=True)
    (srv / "paper.jar").write_bytes(b"server")
    (srv / "plugins" / "A.jar").write_bytes(b"aaa")
    (srv / "plugins" / "B.jar").write_bytes(b"bbb")
    (srv / "plugins" / "notes.txt").write_text("x")
    (srv / "version_history.json").write_text(VERSION)
    monkeypatch.setattr(boottest, "CACHE_DIR", tmp_path / "cache")
    return srv


def test_second_fetch_only_downloads_changed_jars(server, tmp_path):
    fetch_jars(TARGET, CountingRemote(server))
    (server / "plugins" / "B.jar").write_bytes(b"bbbb-updated")
    (server / "plugins" / "A.jar").unlink()
    remote = CountingRemote(server)
    fetch_jars(TARGET, remote)
    assert [r for r in remote.reads if r.endswith(".jar")] == ["plugins/B.jar"]
    cache = tmp_path / "cache" / "jars" / TARGET.name
    assert sorted(p.name for p in (cache / "plugins").glob("*.jar")) == ["B.jar"]
    assert (cache / "plugins" / "B.jar").read_bytes() == b"bbbb-updated"


def test_server_jar_must_match_exactly_one_file(server):
    (server / "paperclip.jar").write_bytes(b"old")
    with pytest.raises(McDeployError, match="must match exactly one file"):
        fetch_jars(TARGET, LocalRemote(server))


def test_server_jar_may_live_in_a_subfolder(server, tmp_path):
    (server / "versions" / "26.3").mkdir(parents=True)
    (server / "versions" / "26.3" / "paper-26.3.jar").write_bytes(b"real")
    target = Target(**{**TARGET.__dict__, "server_jar": "versions/26.3/paper-*.jar"})
    fetch_jars(target, LocalRemote(server))
    assert (tmp_path / "cache" / "jars" / TARGET.name / "server.jar").read_bytes() == b"real"


def test_version_falls_back_to_the_server_log(server, tmp_path):
    os.remove(server / "version_history.json")
    (server / "logs").mkdir()
    (server / "logs" / "latest.log").write_text(
        "[10:50:01 INFO]: Starting minecraft server version 26.3\n"
        "[10:50:01 INFO]: This server is running Paper version 26.3-214-main@e65c076 (2026) (API)\n"
        "[10:51:00 INFO]: player Steve joined from 203.0.113.9\n")
    fetch_jars(TARGET, LocalRemote(server))
    cache = tmp_path / "cache" / "jars" / TARGET.name
    assert (cache / "server_version.txt").read_text() == "26.3-214-main@e65c076"
    assert not (cache / "latest.log").exists()


def test_unknown_server_version_fails(server):
    os.remove(server / "version_history.json")
    with pytest.raises(McDeployError, match="cannot tell which Paper version"):
        fetch_jars(TARGET, LocalRemote(server))


def test_wrong_paper_build_fails_the_boot_test(monkeypatch):
    monkeypatch.setattr(boottest, "load_log_allowlist", lambda profile: [])
    expected = expected_paper_version(VERSION.encode())
    assert expected == "26.3-214-main@e65c076"
    log = ("[10:00:00 INFO]: This server is running Paper version 26.3-rc-3-1-2307f8f (2026) (API)\n"
           "[10:00:05 INFO]: Done (5.0s)! For help, type \"help\"\n")
    failures, _ = analyse_log(log, TARGET, [], expected)
    assert any("26.3-rc-3-1-2307f8f" in f and "wrong jar" in f for f in failures)
    ok_log = log.replace("26.3-rc-3-1-2307f8f", expected)
    assert analyse_log(ok_log, TARGET, [], expected)[0] == []


def test_stack_trace_lines_with_own_headers_form_one_entry():
    log = ("[10:00:00 WARN]: java.lang.NoSuchMethodError: boom\n"
           "[10:00:00 WARN]: \tat a.b.C.d(C.java:1)\n"
           "[10:00:00 WARN]: \t... 5 more\n"
           "[10:00:01 INFO]: next\n")
    entries = parse_log(log)
    assert len(entries) == 2 and entries[0].text.count("\n") == 2
