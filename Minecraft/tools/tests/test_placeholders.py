import pytest

from mcdeploy.errors import McDeployError
from mcdeploy.placeholders import find_tokens, mask, quote_context, render_file

PW = 'p@ss!w"o\'rd\\x'


def test_yaml_double_quoted_value_with_special_chars_round_trips():
    text = 'db:\n  password: "__SECRET_DB_PASSWORD__"  # comment kept\n'
    out = render_file("plugins/X/config.yml", text, {"DB_PASSWORD": PW})
    assert "# comment kept" in out
    from mcdeploy.formats import parse
    assert parse("yaml", out)[0]["db"]["password"] == PW


def test_yaml_single_quoted_value_escapes_quote():
    out = render_file("a.yml", "k: '__SECRET_X__'\n", {"X": "it's"})
    assert out == "k: 'it''s'\n"


def test_yaml_bare_placeholder_rejects_unsafe_value():
    with pytest.raises(McDeployError, match="needs quoting"):
        render_file("a.yml", "k: __SECRET_X__\n", {"X": "!secret"})


def test_yaml_bare_placeholder_rejects_yaml11_boolean():
    with pytest.raises(McDeployError, match="needs quoting"):
        render_file("a.yml", "k: __SECRET_X__\n", {"X": "yes"})


def test_yaml_bare_numeric_value_is_accepted():
    assert render_file("a.yml", "port: __SECRET_PORT__\n", {"PORT": "3306"}) == "port: 3306\n"


def test_partial_placeholder_inside_string():
    out = render_file("velocity.toml", '[servers]\nlobby = "__SECRET_LOBBY_HOST__:25565"\n', {"LOBBY_HOST": "10.0.0.5"})
    assert out == '[servers]\nlobby = "10.0.0.5:25565"\n'


def test_toml_literal_string_cannot_hold_quote():
    with pytest.raises(McDeployError, match="TOML literal"):
        render_file("a.toml", "k = '__SECRET_X__'\n", {"X": "a'b"})


def test_json_escaping():
    from mcdeploy.formats import parse
    out = render_file("a.json", '{"token": "__SECRET_T__"}', {"T": 'a"b\\c'})
    assert parse("json", out)["token"] == 'a"b\\c'


def test_hocon_quoted():
    from mcdeploy.formats import parse
    out = render_file("a.conf", 'password="__SECRET_P__"\n', {"P": PW})
    assert parse("hocon", out)["password"] == PW


def test_properties_backslash_escaped():
    from mcdeploy.formats import parse
    out = render_file("a.properties", "secret=__SECRET_S__\n", {"S": "a\\b"})
    assert parse("properties", out)["secret"] == "a\\b"


def test_missing_value_fails():
    with pytest.raises(McDeployError, match="no value for placeholder X"):
        render_file("a.yml", 'k: "__SECRET_X__"\n', {})


def test_control_characters_rejected():
    with pytest.raises(McDeployError, match="control characters"):
        render_file("a.yml", 'k: "__SECRET_X__"\n', {"X": "a\nb"})


def test_malformed_token_detected():
    uses, problems = find_tokens("a.yml", "k: __SECRET_lower__\n")
    assert not uses and problems


def test_placeholder_in_key_rejected():
    with pytest.raises(McDeployError, match="not allowed in keys"):
        render_file("a.yml", '"__SECRET_K__": 1\n', {"K": "name"})


def test_quote_context():
    assert quote_context('k: "abc', "yaml") == '"'
    assert quote_context("k: 'it''s ", "yaml") == "'"
    assert quote_context('k: "a" b ', "yaml") == "bare"
    assert quote_context('k = "a', "properties") == "bare"


def test_mask_longest_first():
    assert mask("pw=abc123 short=abc", {"__SECRET_LONG__": "abc123", "__SECRET_SHORT__": "abc"}) == \
        "pw=__SECRET_LONG__ short=__SECRET_SHORT__"
