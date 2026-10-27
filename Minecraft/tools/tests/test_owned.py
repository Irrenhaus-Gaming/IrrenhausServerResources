import json

import pytest

import mcdeploy.config as config
import mcdeploy.deploy as deploy
import mcdeploy.pull as pull
from mcdeploy.config import Target
from mcdeploy.errors import McDeployError
from mcdeploy.owned import owned_keys, strip_owned, with_values_from
from mcdeploy.remote import LocalRemote

FILE = "plugins/DecentHolograms/holograms/lb_test.yml"
KEYS = ("location", "facing")
REPO_TEXT = (
    "location: world:0.0:0.0:0.0\n"
    "enabled: true\n"
    "facing: 0.0\n"
    "pages:\n"
    "- lines:\n"
    "  - content: '__STYLE_PRIMARY__Top'\n"
)
MOVED = "location: world:12.5:70.0:-3.5\n"


def rendered(text=REPO_TEXT):
    return text.replace("__STYLE_PRIMARY__", "&e")


def moved(text):
    return text.replace("location: world:0.0:0.0:0.0\n", MOVED).replace("facing: 0.0\n", "facing: 90.0\n")


def test_owned_keys_match_globs():
    target = make_target()
    assert owned_keys(target, FILE) == KEYS
    assert owned_keys(target, "plugins/DecentHolograms/config.yml") == ()


def test_strip_owned_removes_scalar_and_nested_blocks():
    text = b"location:\n  world: w\n  x: 1.0\nname: a\nfacing: 0.0\nlist:\n- a\n"
    assert strip_owned(text, KEYS) == b"name: a\nlist:\n- a\n"
    assert strip_owned(text, ()) == text


def test_with_values_from_takes_only_owned_blocks():
    data = rendered().encode()
    server = moved(rendered()).replace("enabled: true", "enabled: false").encode()
    assert with_values_from(data, server, KEYS) == moved(rendered()).encode()


def test_key_that_occurs_twice_is_not_owned():
    data = b"location: a\nlocation: b\n"
    assert strip_owned(data, KEYS) == data
    assert with_values_from(data, b"location: c\n", KEYS) == data


def test_binary_data_is_untouched():
    data = b"\xff\xfelocation: a\n"
    assert strip_owned(data, KEYS) == data
    assert with_values_from(data, b"location: c\n", KEYS) == data


def test_server_owned_must_be_a_mapping_of_key_lists(tmp_path, monkeypatch):
    mc_dir = tmp_path / "Minecraft"
    for d in ("Proxy", "Backend"):
        (mc_dir / d).mkdir(parents=True)
    profile = ("  source: {src}\n  platform: paper\n  java_image: x\n  server_jar: x\n  accept_eula: true\n"
               "  memory: 1G\n  boot_timeout_seconds: 1\n  settle_seconds: 0\n")
    (mc_dir / "targets.yml").write_text(
        "proxy:\n  environment: mc-proxy\n" + profile.format(src="Proxy")
        + "backend:\n  environment_prefix: mc-backend-\n" + profile.format(src="Backend")
        + "  server_owned:\n    \"a/*.yml\": location\nlisted_server_prefix: mc-server-\n"
    )
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "TARGETS_FILE", mc_dir / "targets.yml")
    with pytest.raises(McDeployError, match="server_owned"):
        config.load_layout()


def make_target():
    return Target(
        name="mc-backend-Test", profile="backend", server_name="Test", source="Backend", platform="paper",
        java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
        server_owned=(("plugins/DecentHolograms/holograms/*.yml", KEYS),),
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    mc_dir = tmp_path / "repo" / "Minecraft"
    src = mc_dir / "Backend"
    (src / FILE).parent.mkdir(parents=True)
    (src / FILE).write_bytes(REPO_TEXT.encode())
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    for module in (deploy, pull):
        monkeypatch.setattr(module, "managed_files", lambda target, include_untracked=True: [FILE])
        monkeypatch.setattr(module, "target_placeholders", lambda target: {"PRIMARY": "style"})
        monkeypatch.setattr(module, "load_mask_scopes", lambda: {})
        monkeypatch.setattr(module, "load_expansions", lambda: {})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ACT", raising=False)
    return make_target(), LocalRemote(server), server


def run(target, remote, **kw):
    deploy.run_deploy(target, dry_run=False, remote=remote, values={}, **kw)


def edit_repo(target, old="enabled: true", new="enabled: false"):
    path = target.source_dir / FILE
    path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))


def test_first_install_uses_repo_default(env):
    target, remote, server = env
    run(target, remote)
    assert (server / FILE).read_bytes().decode() == rendered()
    manifest = json.loads((server / deploy.MANIFEST).read_bytes().decode())
    assert manifest["owned"] == {FILE: list(KEYS)}


def test_moved_hologram_keeps_server_position_on_update(env):
    target, remote, server = env
    run(target, remote)
    (server / FILE).write_bytes(moved(rendered()).encode())  # /dh movehere + /dh setfacing
    edit_repo(target)
    run(target, remote)
    assert (server / FILE).read_bytes().decode() == moved(rendered()).replace("enabled: true", "enabled: false")


def test_moved_hologram_alone_is_neither_drift_nor_uploaded(env):
    target, remote, server = env
    run(target, remote)
    (server / FILE).write_bytes(moved(rendered()).encode())
    run(target, remote, full=True)
    assert (server / FILE).read_bytes().decode() == moved(rendered())


def test_other_server_edit_still_aborts(env):
    target, remote, server = env
    run(target, remote)
    (server / FILE).write_bytes(moved(rendered()).replace("enabled: true", "enabled: maybe").encode())
    edit_repo(target)
    with pytest.raises(McDeployError, match="changed on the server"):
        run(target, remote)


def test_existing_hologram_on_new_managed_path_is_taken_over_with_its_position(env):
    target, remote, server = env
    (server / FILE).parent.mkdir(parents=True)
    (server / FILE).write_bytes(moved(rendered()).encode())
    run(target, remote)
    assert (server / FILE).read_bytes().decode() == moved(rendered())
    assert (server / deploy.MANIFEST).is_file()


def test_manifest_from_before_server_owned_keys_is_migrated(env):
    target, remote, server = env
    (server / FILE).parent.mkdir(parents=True)
    (server / FILE).write_bytes(moved(rendered()).encode())
    old = {"commit": "old", "files": {FILE: deploy.sha256(rendered().encode())}}  # whole-file hash, no "owned"
    (server / deploy.MANIFEST).write_bytes(json.dumps(old).encode())
    edit_repo(target)
    run(target, remote)
    assert (server / FILE).read_bytes().decode() == moved(rendered()).replace("enabled: true", "enabled: false")
    assert json.loads((server / deploy.MANIFEST).read_bytes().decode())["owned"] == {FILE: list(KEYS)}


def test_pull_keeps_repo_default_for_owned_keys(env, monkeypatch):
    target, remote, server = env
    run(target, remote)
    (server / FILE).write_bytes(moved(rendered()).replace("enabled: true", "enabled: false").encode())
    monkeypatch.setattr(pull, "deploy_values", lambda: {})
    monkeypatch.setattr(pull, "load_schema", lambda: {})
    monkeypatch.setattr(pull, "load_scan_allowlist", lambda: [])
    monkeypatch.setattr(pull, "SftpRemote", lambda values: remote)
    monkeypatch.setattr(pull, "verify_instance", lambda target, remote, values: None)
    pull.run_pull(target, [])
    assert (target.source_dir / FILE).read_bytes().decode() == REPO_TEXT.replace("enabled: true", "enabled: false")
