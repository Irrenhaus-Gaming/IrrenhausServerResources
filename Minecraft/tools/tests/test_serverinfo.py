from dataclasses import replace

import pytest

import mcdeploy.config as config
import mcdeploy.deploy as deploy
from mcdeploy.config import Target
from mcdeploy.deploy import Masker, render_text, resolve_values
from mcdeploy.errors import McDeployError
from mcdeploy.placeholders import find_tokens
from mcdeploy.remote import LocalRemote
from mcdeploy.serverinfo import check_server_tokens

FILE = "plugins/Eco/engine.yml"
REPO_TEXT = "Database:\n  MySQL:\n    ServerId: __SERVER_NAME__\n    Password: \"__SECRET_DB_PW__\"\n"
NAMES = {"DB_PW": "secret", "NAME": "server"}
TARGET = Target(
    name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
    java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
)


def test_server_name_comes_from_the_target():
    assert resolve_values(TARGET, NAMES, {"DB_PW": "pw"}, "t") == {"DB_PW": "pw", "NAME": "Test"}


def test_target_without_server_name_fails():
    with pytest.raises(McDeployError, match="no server name"):
        resolve_values(replace(TARGET, server_name=""), NAMES, {"DB_PW": "pw"}, "t")


@pytest.mark.parametrize("text, profile, message", [
    ("id: __SERVER_FOO__\n", "backend", "unknown __SERVER_FOO__"),
    ("id: __SERVER_NAME__\n", "proxy", "backend only"),
])
def test_invalid_server_tokens(text, profile, message):
    uses, problems = find_tokens("f.yml", text)
    assert not problems
    assert any(message in p for p in check_server_tokens(uses, profile))


def test_pull_masking_restores_the_token():
    values = {"DB_PW": "pw", "NAME": "Test"}
    server = "Database:\n  MySQL:\n    ServerId: Test\n    Password: \"pw\"\nTest: Test\n"
    masked = Masker(NAMES, values, {}).apply(FILE, server, REPO_TEXT)
    # Only the exact repo line gets the token back; the name is never replaced elsewhere.
    assert masked == REPO_TEXT + "Test: Test\n"
    assert render_text(FILE, masked, values, {}) == server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    mc_dir = tmp_path / "repo" / "Minecraft"
    src = mc_dir / "Backend"
    (src / "plugins/Eco").mkdir(parents=True)
    (src / FILE).write_text(REPO_TEXT)
    server = tmp_path / "server"
    (server / "plugins/Eco").mkdir(parents=True)
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(deploy, "managed_files", lambda target, include_untracked=True: [FILE])
    monkeypatch.setattr(deploy, "target_placeholders", lambda target: dict(NAMES))
    monkeypatch.setattr(deploy, "load_mask_scopes", lambda: {})
    monkeypatch.setattr(deploy, "load_expansions", lambda: {})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ACT", raising=False)
    return LocalRemote(server), server


def test_deploy_writes_the_server_name(env):
    remote, server = env
    deploy.run_deploy(TARGET, dry_run=False, remote=remote, values={"DB_PW": "pw"})
    assert "ServerId: Test\n" in (server / FILE).read_text()
    # A later deploy with a changed secret sees no drift.
    deploy.run_deploy(TARGET, dry_run=False, remote=remote, values={"DB_PW": "pw2"})
    text = (server / FILE).read_text()
    assert "ServerId: Test\n" in text and '"pw2"' in text


def test_server_name_is_not_masked_in_actions_logs(env, monkeypatch, capsys):
    remote, _ = env
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    deploy.run_deploy(TARGET, dry_run=True, remote=remote, values={"DB_PW": "pw"})
    out = capsys.readouterr().out
    assert "::add-mask::pw" in out and "::add-mask::Test" not in out
