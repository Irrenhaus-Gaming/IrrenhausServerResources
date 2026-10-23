import json
import tomllib

import pytest

from mcdeploy.blocks import expand_blocks, parse_entries
from mcdeploy.deploy import Masker, render_text
from mcdeploy.errors import McDeployError

EXP = {"MC_SERVERS": "toml-entries"}
TOML = (
    "[servers]\n"
    "\t# comment\n"
    "\t__SECRET_MC_SERVERS__\n"
    '\ttry = ["Lobby"]\n'
    "\n"
    "[query]\n"
    "\tport = 25565\n"
)
SERVERS = json.dumps({"Lobby": "10.0.0.5:25565", "Adventure": "10.0.0.6:25566"})


def test_expands_into_table_entries_in_order():
    out = render_text("velocity.toml", TOML, {"MC_SERVERS": SERVERS}, EXP)
    assert '\tLobby = "10.0.0.5:25565"\n\tAdventure = "10.0.0.6:25566"\n\ttry = ["Lobby"]' in out
    doc = tomllib.loads(out)
    assert doc["servers"] == {"Lobby": "10.0.0.5:25565", "Adventure": "10.0.0.6:25566", "try": ["Lobby"]}


def test_new_server_needs_no_schema_change():
    more = json.dumps({"Lobby": "a:1", "Adventure": "b:2", "NewWorld": "c:3"})
    assert tomllib.loads(render_text("velocity.toml", TOML, {"MC_SERVERS": more}, EXP))["servers"]["NewWorld"] == "c:3"


@pytest.mark.parametrize("raw, message", [
    ("not json", "not valid JSON"),
    ("{}", "non-empty JSON object"),
    ('["a"]', "non-empty JSON object"),
    ('{"bad name": "x:1"}', "may only contain"),
    ('{"A": ""}', "non-empty single-line"),
    ('{"A": "x\\ny"}', "non-empty single-line"),
    ('{"A": "x:1", "A": "y:2"}', "listed twice"),
])
def test_invalid_values_fail(raw, message):
    with pytest.raises(McDeployError, match=message):
        parse_entries("MC_SERVERS", raw)


def test_duplicate_of_static_key_fails_verification():
    text = '[servers]\n\tLobby = "1.1.1.1:1"\n\t__SECRET_MC_SERVERS__\n'
    with pytest.raises(McDeployError):
        render_text("velocity.toml", text, {"MC_SERVERS": SERVERS}, EXP)


def test_placeholder_must_stand_alone_and_only_in_toml():
    with pytest.raises(McDeployError, match="alone"):
        expand_blocks("velocity.toml", 'x = "__SECRET_MC_SERVERS__"\n', {"MC_SERVERS": SERVERS}, EXP)
    with pytest.raises(McDeployError, match="only supported in TOML"):
        expand_blocks("a.yml", "__SECRET_MC_SERVERS__\n", {"MC_SERVERS": SERVERS}, EXP)


def test_missing_value_fails():
    with pytest.raises(McDeployError, match="no value"):
        expand_blocks("velocity.toml", TOML, {}, EXP)


def test_masker_collapses_unchanged_block_back_to_placeholder():
    values = {"MC_SERVERS": SERVERS}
    server = render_text("velocity.toml", TOML, values, EXP)
    masker = Masker({"MC_SERVERS": "secret"}, values, {}, EXP)
    assert masker.apply("velocity.toml", server, TOML) == TOML


def test_masker_leaves_changed_block_for_the_scanner():
    values = {"MC_SERVERS": SERVERS}
    server = render_text("velocity.toml", TOML, values, EXP).replace("10.0.0.6", "10.0.0.9")
    masker = Masker({"MC_SERVERS": "secret"}, values, {}, EXP)
    assert "__SECRET_MC_SERVERS__" not in masker.apply("velocity.toml", server, TOML)


def test_sync_back_reads_server_list_from_server_file():
    from mcdeploy.blocks import entries_from_server
    server = render_text("velocity.toml", TOML, {"MC_SERVERS": SERVERS}, EXP)
    assert json.loads(entries_from_server("velocity.toml", server, TOML, "MC_SERVERS")) == json.loads(SERVERS)
    assert entries_from_server("other.toml", server, "[servers]\n", "MC_SERVERS") is None
