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


def change_repo_file(target, extra="new: 1\n"):
    (target.source_dir / FILE).write_bytes((REPO_TEXT + extra).encode())


def test_server_edit_after_deploy_aborts_with_masked_diff(env, capsys):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).write_text(rendered_text() + "extra: true\n")
    change_repo_file(target)  # the commit touches the file, so the deploy must look at it
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


def test_deleted_managed_file_counts_as_drift_when_touched_or_in_full_check(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).unlink()
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})  # untouched: ignored
    assert not (server / FILE).exists()
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"}, full=True)


class CountingLocalRemote(LocalRemote):
    def __init__(self, root):
        super().__init__(root)
        self.reads = []

    def read(self, rel):
        self.reads.append(rel)
        return super().read(rel)


def test_unchanged_files_are_neither_read_nor_uploaded(env):
    target, _, server = env
    deploy.run_deploy(target, dry_run=False, remote=LocalRemote(server), values={"DB_PW": "hunter2!"})
    remote = CountingLocalRemote(server)
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    assert remote.reads == [deploy.MANIFEST]


def test_server_edit_to_untouched_file_is_preserved_and_found_by_full_check(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).write_text("edited on the server\n")
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    assert (server / FILE).read_text() == "edited on the server\n"
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"}, full=True)


def test_changed_secret_redeploys_the_file(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "rotated-pw"})
    assert (server / FILE).read_text() == rendered_text("rotated-pw")


def test_half_finished_deploy_is_not_drift(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    change_repo_file(target)
    (server / FILE).write_bytes((rendered_text() + "new: 1\n").encode())  # uploaded, manifest not written
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    manifest = json.loads((server / deploy.MANIFEST).read_text())
    assert manifest["files"][FILE] == deploy.sha256((rendered_text() + "new: 1\n").encode())


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
    change_repo_file(target)
    with pytest.raises(McDeployError):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    leaking = [l for l in capsys.readouterr().out.splitlines() if "hunter2!" in l and not l.startswith("::add-mask::")]
    assert leaking == []


def test_adopt_replaces_defaults_on_a_never_deployed_server(env):
    target, remote, server = env
    (server / FILE).parent.mkdir(parents=True)
    (server / FILE).write_text("db:\n  password: default\n")
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"}, adopt=True)
    assert (server / FILE).read_text() == rendered_text()
    assert (server / deploy.MANIFEST).is_file()


def test_adopt_is_refused_once_a_manifest_exists(env):
    target, remote, server = env
    deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"})
    (server / FILE).write_text("edited on the server\n")
    change_repo_file(target)
    with pytest.raises(McDeployError, match="only for a server that was never deployed to"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={"DB_PW": "hunter2!"}, adopt=True)
    assert (server / FILE).read_text() == "edited on the server\n"


def test_repo_only_files_are_not_managed(monkeypatch):
    files = ["plugins/FMM/models/pet.bbmodel", "plugins/FMM/models/pet.source.md", "plugins/Plug/config.yml"]
    monkeypatch.setattr(config, "source_files", lambda source, include_untracked=True: files)
    monkeypatch.setattr(config, "generated_files", lambda target: {})
    target = Target(
        name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
        java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
        repo_only=("*.source.md",),
    )
    assert config.managed_files(target) == ["plugins/FMM/models/pet.bbmodel", "plugins/Plug/config.yml"]


def test_repo_only_must_be_a_list_of_globs(tmp_path, monkeypatch):
    mc_dir = tmp_path / "Minecraft"
    for d in ("Proxy", "Backend"):
        (mc_dir / d).mkdir(parents=True)
    profile = ("  source: {src}\n  platform: paper\n  java_image: x\n  server_jar: x\n  accept_eula: true\n"
               "  memory: 1G\n  boot_timeout_seconds: 1\n  settle_seconds: 0\n")
    (mc_dir / "targets.yml").write_text(
        "proxy:\n  environment: mc-proxy\n" + profile.format(src="Proxy")
        + "backend:\n  environment_prefix: mc-backend-\n" + profile.format(src="Backend")
        + "  repo_only: \"*.source.md\"\nlisted_server_prefix: mc-server-\n"
    )
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "TARGETS_FILE", mc_dir / "targets.yml")
    with pytest.raises(McDeployError, match="repo_only"):
        config.load_layout()


def test_model_credits_stay_in_the_repo():
    target = config.get_target("mc-backend-Test")
    assert "*.source.md" in target.repo_only
