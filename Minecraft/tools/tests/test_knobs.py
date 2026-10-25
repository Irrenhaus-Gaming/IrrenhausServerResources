import pytest

import mcdeploy.config as config
from mcdeploy.deploy import Masker, render_text, resolve_values
from mcdeploy.errors import McDeployError
from mcdeploy.formats import parse
from mcdeploy.knobs import knob_values, load_knobs
from mcdeploy.placeholders import find_tokens

KNOBS = {
    "style": {"PRIMARY": "&e", "TEXT": "&f"},
    "rewards": {
        "MONEY": {"multiplier": 3, "text": "{PRIMARY}{amount} Moneten"},
        "XP": {"multiplier": 1.5, "text": "&#F15BB5{amount}◎"},
    },
}
QUEST = (
    'lore:\n'
    '- "__STYLE_TEXT__ • Belohnung: __REWARD_MONEY_TEXT_750__"\n'
    'startstring:\n'
    '- "__STYLE_TEXT__ • um __REWARD_MONEY_TEXT_750__ __STYLE_TEXT__zu erhalten."\n'
    'rewards:\n'
    '- "moneten give {player} __REWARD_MONEY_750__ -s"\n'
)


def values_for(text):
    uses, _ = find_tokens("q.yml", text)
    return knob_values({u.name: u.kind for u in uses}, load_knobs(KNOBS))


def test_style_and_reward_values():
    assert knob_values(
        {"PRIMARY": "style", "MONEY_750": "reward", "MONEY_TEXT_750": "reward", "XP_TEXT_5": "reward"},
        load_knobs(KNOBS),
    ) == {"PRIMARY": "&e", "MONEY_750": "2250", "MONEY_TEXT_750": "&e2250 Moneten", "XP_TEXT_5": "&#F15BB58◎"}


def test_rewards_round_half_up():
    assert knob_values({"XP_1": "reward", "XP_3": "reward"}, load_knobs(KNOBS)) == {"XP_1": "2", "XP_3": "5"}


@pytest.mark.parametrize("names, message", [
    ({"NOPE": "style"}, "no style 'NOPE'"),
    ({"GEMS_10": "reward"}, "no reward kind 'GEMS'"),
    ({"MONEY": "reward"}, "expected __REWARD_<KIND>_<amount>__"),
    ({"MONEY_0": "reward"}, "rounds to 0"),
])
def test_unknown_or_invalid_knobs_fail(names, message):
    with pytest.raises(McDeployError, match=message):
        knob_values(names, load_knobs(KNOBS))


@pytest.mark.parametrize("data, message", [
    ({"style": {"lower": "&e"}}, "names must match"),
    ({"rewards": {"MY_KIND": {"multiplier": 1, "text": "{amount}"}}}, "no underscore"),
    ({"rewards": {"MONEY": {"multiplier": 0, "text": "{amount}"}}}, "multiplier must be a number > 0"),
    ({"rewards": {"MONEY": {"multiplier": True, "text": "{amount}"}}}, "multiplier must be a number > 0"),
    ({"rewards": {"MONEY": {"multiplier": 1, "text": "Moneten"}}}, "containing {amount}"),
    ({"rewards": {"MONEY": {"multiplier": 1, "text": "{GOLD}{amount}"}}}, "not a style name"),
    ({"colors": {}}, "unknown top-level keys"),
])
def test_invalid_knob_file_fails(data, message):
    with pytest.raises(McDeployError, match=message):
        load_knobs(data)


def test_quest_renders_and_parses():
    out = render_text("q.yml", QUEST, values_for(QUEST), {})
    doc = parse("yaml", out)[0]
    assert doc["lore"] == ["&f • Belohnung: &e2250 Moneten"]
    assert doc["startstring"] == ["&f • um &e2250 Moneten &fzu erhalten."]
    assert doc["rewards"] == ["moneten give {player} 2250 -s"]


def test_masker_restores_knob_lines_but_never_replaces_knob_values_globally():
    values = values_for(QUEST)
    names = {n: "reward" if "_" in n else "style" for n in values}
    server = render_text("q.yml", QUEST, values, {}) + 'extra: "&e2250 by hand"\n'
    masked = Masker(names, values, {}).apply("q.yml", server, QUEST)
    assert masked == QUEST + 'extra: "&e2250 by hand"\n'


def test_resolve_values_needs_secrets_but_computes_knobs(monkeypatch):
    monkeypatch.setattr("mcdeploy.deploy.knob_values", lambda names: knob_values(names, load_knobs(KNOBS)))
    names = {"DB_PW": "secret", "PRIMARY": "style"}
    assert resolve_values(names, {"DB_PW": "x"}, "t") == {"DB_PW": "x", "PRIMARY": "&e"}
    with pytest.raises(McDeployError, match="DB_PW"):
        resolve_values(names, {}, "t")


def test_lowercase_knob_token_is_malformed():
    _, problems = find_tokens("q.yml", 'k: "__STYLE_primary__"\n')
    assert problems


def test_repo_knob_file_is_valid():
    load_knobs()
    assert config.KNOBS_FILE.is_file()
