import json

import pytest

import mcdeploy.config as config
import mcdeploy.deploy as deploy
from mcdeploy.config import Target
from mcdeploy.errors import McDeployError
from mcdeploy.remote import LocalRemote

FILE = "plugins/Plug/config.yml"
REPO_TEXT = 'db:\n  password: "__SECRET_DB_PW__"\n'


@pytest.fixture()
def env(tmp_path, monkeypatch):
    mc_dir = tmp_path / "repo" / "Minecraft"
    src = mc_dir / "Backend"
    (src / "plugins/Plug").mkdir(parents=True)
    (src / FILE).write_text(REPO_TEXT)
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(deploy, "managed_files", lambda target, include_untracked=True: [FILE])
    monkeypatch.setattr(deploy, "target_placeholders", lambda target: {"DB_PW": "secret"})
    monkeypatch.setattr(deploy, "load_mask_scopes", lambda: {})
    monkeypatch.setattr(deploy, "load_expansions", lambda: {})
    # Outside GitHub Actions no ::add-mask:: lines are printed; the CI case has its own test.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ACT", raising=False)
    target = Target(
        name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
        java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
    )
    return target, LocalRemote(server), server


def rendered_text(pw="hunter2!"):
    return f'db:\n  password: "{pw}"\n'


def test_first_deploy_uploads_and_writes_manifest(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    assert (server / FILE).read_text() == rendered_text()
    manifest = json.loads((server / deploy.MANIFEST).read_text())
    assert FILE in manifest["files"]


def test_missing_secret_fails_before_touching_server(env):
    target, remote, server = env
    with pytest.raises(McDeployError, match="DB_PW"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={})
    assert not any(server.iterdir())


def test_server_edit_after_deploy_aborts_with_masked_diff(env, capsys):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).write_text(rendered_text() + "extra: true\n")
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    out = capsys.readouterr().out
    assert "+extra: true" in out
    assert "hunter2!" not in out  # secret is masked in the diff
    assert (server / FILE).read_text().endswith("extra: true\n")  # nothing overwritten


def test_existing_unmanaged_identical_file_is_adopted(env):
    target, remote, server = env
    (server / FILE).parent.mkdir(parents=True)
    (server / FILE).write_text(rendered_text())
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    assert (server / deploy.MANIFEST).is_file()


def test_existing_unmanaged_different_file_aborts(env):
    target, remote, server = env
    (server / FILE).parent.mkdir(parents=True)
    (server / FILE).write_text(rendered_text("old-password"))
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})


def test_deleted_managed_file_counts_as_drift(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).unlink()
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})


def test_dry_run_writes_nothing(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=True, remote=remote, values={"DB_PW": "hunter2!"})
    assert not any(server.iterdir())


def test_act_forces_dry_run(env, monkeypatch):
    target, remote, server = env
    monkeypatch.setenv("ACT", "true")
    monkeypatch.delenv("MC_ACT_ALLOW_DEPLOY", raising=False)
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    assert not any(server.iterdir())


def test_in_github_actions_values_appear_only_in_add_mask_commands(env, monkeypatch, capsys):
    target, remote, server = env
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).write_text(rendered_text() + "extra: true\n")
    with pytest.raises(McDeployError):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    leaking = [l for l in capsys.readouterr().out.splitlines() if "hunter2!" in l and not l.startswith("::add-mask::")]
    assert leaking == []
