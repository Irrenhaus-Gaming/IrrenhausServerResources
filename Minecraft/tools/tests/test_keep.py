import uuid

import pytest

import mcdeploy.config as config
import mcdeploy.deploy as deploy
from mcdeploy.config import Target
from mcdeploy.errors import McDeployError
from mcdeploy.keep import keep_lines, keep_values
from mcdeploy.remote import LocalRemote

FILE = "plugins/Eco/engine.yml"
REPO_TEXT = "Database:\n  MySQL:\n    ServerId: __KEEP_ECO_SERVER_ID__\n    Password: \"__SECRET_DB_PW__\"\n"
SERVER_ID = "75c3ffad-2006-4ea7-a3b6-8fafc7c5c548"


def read_from(text):
    return lambda rel: None if text is None else text.encode()


@pytest.mark.parametrize("server_line", [f"    ServerId: {SERVER_ID}", f"    ServerId: '{SERVER_ID}'", f'    ServerId: "{SERVER_ID}"'])
def test_value_is_read_from_the_server_line(server_line):
    server = f"Database:\n  MySQL:\n{server_line}\n    Password: x\n"
    assert keep_values({FILE: REPO_TEXT}, read_from(server)) == {"ECO_SERVER_ID": SERVER_ID}


def test_missing_server_file_gets_a_new_uuid():
    value = keep_values({FILE: REPO_TEXT}, read_from(None))["ECO_SERVER_ID"]
    assert uuid.UUID(value)


def test_missing_server_line_fails():
    with pytest.raises(McDeployError, match="found no line"):
        keep_values({FILE: REPO_TEXT}, read_from("Database:\n  Type: SQLITE\n"))


def test_keep_token_must_be_alone_on_its_line():
    with pytest.raises(McDeployError, match="only placeholder"):
        keep_lines(FILE, 'k: "__KEEP_A__-__SECRET_B__"\n')


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
    monkeypatch.setattr(deploy, "target_placeholders", lambda target: {"DB_PW": "secret", "ECO_SERVER_ID": "keep"})
    monkeypatch.setattr(deploy, "load_mask_scopes", lambda: {})
    monkeypatch.setattr(deploy, "load_expansions", lambda: {})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ACT", raising=False)
    target = Target(
        name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
        java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
    )
    return target, LocalRemote(server), server


def test_deploy_keeps_the_server_generated_id(env):
    target, remote, server = env
    # The plugin's own file: same id, other settings still default. Deployed once as-is (adopt), the id survives.
    (server / FILE).write_text(f"Database:\n  MySQL:\n    ServerId: {SERVER_ID}\n    Password: ''\n")
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "pw"}, adopt=True)
    assert f"ServerId: {SERVER_ID}\n" in (server / FILE).read_text()
    # A later deploy with a changed secret reads it again and does not see drift.
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "pw2"})
    text = (server / FILE).read_text()
    assert f"ServerId: {SERVER_ID}\n" in text and '"pw2"' in text
