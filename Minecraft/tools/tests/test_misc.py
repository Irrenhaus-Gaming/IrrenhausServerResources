import pytest

import mcdeploy.boottest as boottest
from mcdeploy.boottest import AllowRule, analyse_log, parse_log
from mcdeploy.config import Target
from mcdeploy.convert import _mask_line
from mcdeploy.errors import McDeployError
from mcdeploy.formats import parse_properties
from mcdeploy.lint import forbidden_reason
from mcdeploy.pull import requote
from mcdeploy.scan import scan_text

TARGET = Target(
    name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
    java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
)

LOG = """\
[10:00:00 INFO]: [CoreProtect] Enabling CoreProtect v24.1
[10:00:01 WARN]: Legacy plugin detected
[10:00:02 ERROR]: [EliteMobs] Could not reach nightbreak
[10:00:03 INFO]: Something failed
java.lang.IllegalStateException: boom
\tat foo.Bar.baz(Bar.java:1)
[10:00:04 INFO]: Done (12.345s)! For help, type "help"
"""


def test_log_analysis_flags_errors_and_stack_traces(monkeypatch):
    monkeypatch.setattr(boottest, "load_log_allowlist", lambda target: [])
    failures, warnings = analyse_log(LOG, TARGET, ["CoreProtect", "LuckPerms"])
    assert any("nightbreak" in f for f in failures)
    assert any("IllegalStateException" in f for f in failures)
    assert any("LuckPerms" in f for f in failures)
    assert warnings == ["Legacy plugin detected"]


def test_log_allowlist_suppresses_matching_error(monkeypatch):
    import re
    rules = [AllowRule(("backend-test",), re.compile("nightbreak"), "no account in tests"),
             AllowRule(("backend-test",), re.compile("IllegalStateException: boom"), "known harmless")]
    monkeypatch.setattr(boottest, "load_log_allowlist", lambda target: rules)
    failures, _ = analyse_log(LOG, TARGET, ["CoreProtect"])
    assert failures == []


def test_log_without_done_fails(monkeypatch):
    monkeypatch.setattr(boottest, "load_log_allowlist", lambda target: [])
    failures, _ = analyse_log("[10:00:00 INFO]: starting\n", TARGET, [])
    assert any("Done" in f for f in failures)


def test_parse_log_groups_continuations():
    entries = parse_log(LOG)
    assert entries[3].text.startswith("Something failed\njava.lang")


def test_scan_finds_credentials_but_not_placeholders():
    text = 'mysql-password: hunter2\npassword: "__SECRET_X__"\nreadyCheckTokenHint: ready\nhost: localhost\n'
    hits = scan_text("a.yml", text)
    assert [(h.line, h.key) for h in hits] == [(1, "mysql-password")]


def test_scan_finds_public_ip_and_tokens():
    hits = scan_text("velocity.toml", 'lobby = "203.0.113.7:25565"\nlocal = "127.0.0.1:25566"\n')
    assert [h.reason for h in hits] == ["IP address"]
    assert scan_text("a.yml", "token: nbk_abcdefghijklmnopqrstuv\n")


def test_mask_line_quotes_bare_yaml_values():
    assert _mask_line("pw: hun!ter", "hun!ter", "__SECRET_P__", "yaml", "x") == 'pw: "__SECRET_P__"'
    assert _mask_line('pw: "hun!ter"', "hun!ter", "__SECRET_P__", "yaml", "x") == 'pw: "__SECRET_P__"'
    assert _mask_line('a = "1.2.3.4:25565"', "1.2.3.4", "__SECRET_H__", "toml", "x") == 'a = "__SECRET_H__:25565"'
    assert _mask_line("url: jdbc://1.2.3.4/db", "1.2.3.4", "__SECRET_H__", "yaml", "x") == 'url: "jdbc://__SECRET_H__/db"'
    with pytest.raises(McDeployError):
        _mask_line("a = 1.2.3.4", "1.2.3.4", "__SECRET_H__", "toml", "x")


def test_requote_restores_quotes_after_plugin_rewrite():
    assert requote("password: __SECRET_P__\n", "yaml") == 'password: "__SECRET_P__"\n'
    assert requote('password: "__SECRET_P__"\n', "yaml") == 'password: "__SECRET_P__"\n'


def test_properties_parser():
    assert parse_properties("# c\na=1\nb : two\\\n  more\nc\\=d=e\n") == {"a": "1", "b": "twomore", "c=d": "e"}


@pytest.mark.parametrize("path", ["plugins/x.jar", "plugins/x.jar.uploading-1", "plugins/a/data.db",
                                  "plugins/floodgate/key.pem", "forwarding.secret", "plugins/Q/playerdata/u.yml"])
def test_forbidden_files(path):
    assert forbidden_reason(path)


def test_allowed_file():
    assert forbidden_reason("plugins/Quests/quests/mining/q1.yml") is None

def test_masker_restores_only_exact_repo_lines():
    from mcdeploy.deploy import Masker
    names = {"DB_USER": "secret", "DB_HOST": "var", "DB_PW": "secret"}
    resolved = {"DB_USER": "Minecraft", "DB_HOST": "localhost", "DB_PW": "s3cret!"}
    masker = Masker(names, resolved, {"DB_USER": "keyed"})
    repo = (
        'username: "__SECRET_DB_USER__"\n'
        'host: "__VAR_DB_HOST__"\n'
        "rabbit: localhost\n"
        'password: "__SECRET_DB_PW__"\n'
    )
    server = (
        'username: "Minecraft"\n'
        'host: "localhost"\n'
        "rabbit: localhost\n"
        'password: "s3cret!"\n'
        "motd: Minecraft s3cret!\n"
    )
    assert masker.apply("a.yml", server, repo) == (
        'username: "__SECRET_DB_USER__"\n'
        'host: "__VAR_DB_HOST__"\n'
        "rabbit: localhost\n"
        'password: "__SECRET_DB_PW__"\n'
        "motd: Minecraft __SECRET_DB_PW__\n"
    )


def test_mask_line_ports_and_host_port_pairs():
    assert _mask_line("mysql-port: 3306", "3306", "__VAR_P__", "yaml", "x") == "mysql-port: __VAR_P__"
    assert _mask_line("mysqlPort: '3306'", "3306", "__VAR_P__", "yaml", "x") == "mysqlPort: '__VAR_P__'"
    line = _mask_line("ip: localhost:3306", "localhost", "__VAR_H__", "yaml", "x")
    assert line == 'ip: "__VAR_H__:3306"'
    assert _mask_line(line, "3306", "__VAR_P__", "yaml", "x") == 'ip: "__VAR_H__:__VAR_P__"'


def test_requote_keeps_repo_lines_with_bare_port():
    repo_lines = frozenset(["mysql-port: __SECRET_MC_DB_PORT__"])
    text = "mysql-port: __SECRET_MC_DB_PORT__\npassword: __SECRET_P__\n"
    assert requote(text, "yaml", repo_lines) == 'mysql-port: __SECRET_MC_DB_PORT__\npassword: "__SECRET_P__"\n'
