"""Knob tokens (__STYLE_NAME__ / __REWARD_KIND_N__ / __REWARD_KIND_TEXT_N__) and their values.

Unlike secrets, knob values live in the repo (Minecraft/knobs.yml) and are computed at render time,
so one line there changes every file that uses the knob.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from . import config
from .config import NAME_RE, load_yaml
from .errors import McDeployError, fail_if

KNOB_KINDS = ("style", "reward")
KIND_RE = re.compile(r"^[A-Z0-9]+$")
REWARD_NAME_RE = re.compile(r"^(?P<kind>[A-Z0-9]+)(?P<text>_TEXT)?_(?P<amount>[0-9]+)$")
FIELD_RE = re.compile(r"\{([^{}]*)\}")


def load_knobs(data=None) -> dict:
    """Load and validate knobs.yml (or `data`, for tests). Returns {"style": {...}, "rewards": {...}}."""
    if data is None:
        data = load_yaml(config.KNOBS_FILE) or {}
    problems: list[str] = []
    if not isinstance(data, dict):
        raise McDeployError("knobs.yml must be a mapping")
    unknown = set(data) - {"style", "rewards", "shop"}
    if unknown:
        problems.append(f"unknown top-level keys {sorted(unknown)}")
    style = data.get("style") or {}
    rewards = data.get("rewards") or {}
    if not isinstance(style, dict) or not isinstance(rewards, dict):
        raise McDeployError("knobs.yml: 'style' and 'rewards' must be mappings")
    for name, value in style.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append(f"style {name!r}: names must match {NAME_RE.pattern}")
        if not isinstance(value, str):
            problems.append(f"style {name}: value must be a string")
    for kind, spec in rewards.items():
        if not isinstance(kind, str) or not KIND_RE.match(kind):
            problems.append(f"rewards {kind!r}: names must match {KIND_RE.pattern} (no underscore)")
            continue
        if not isinstance(spec, dict):
            problems.append(f"rewards {kind}: must be a mapping")
            continue
        unknown = set(spec) - {"multiplier", "text"}
        if unknown:
            problems.append(f"rewards {kind}: unknown keys {sorted(unknown)}")
        multiplier = spec.get("multiplier")
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or multiplier <= 0:
            problems.append(f"rewards {kind}: multiplier must be a number > 0")
        text = spec.get("text")
        if not isinstance(text, str) or "{amount}" not in text:
            problems.append(f"rewards {kind}: text must be a string containing {{amount}}")
            continue
        for field in FIELD_RE.findall(text):
            if field != "amount" and field not in style:
                problems.append(f"rewards {kind}: text refers to {{{field}}}, which is not a style name")
    fail_if(problems, "knobs.yml is invalid:")
    # `shop` is validated by mcdeploy.shop (it has its own invariants).
    return {"style": style, "rewards": rewards, "shop": data.get("shop")}


def reward_amount(base: str, multiplier) -> int:
    return int((Decimal(base) * Decimal(str(multiplier))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def knob_values(names: dict[str, str], knobs: dict | None = None) -> dict[str, str]:
    """Value of every knob token. `names` maps token NAME -> kind ("style" or "reward")."""
    if not names:
        return {}
    knobs = knobs or load_knobs()
    style, rewards = knobs["style"], knobs["rewards"]
    values: dict[str, str] = {}
    problems: list[str] = []
    for name, kind in sorted(names.items()):
        if kind == "style":
            if name in style:
                values[name] = style[name]
            else:
                problems.append(f"__STYLE_{name}__: no style '{name}' in knobs.yml")
            continue
        m = REWARD_NAME_RE.match(name)
        if not m:
            problems.append(f"__REWARD_{name}__: expected __REWARD_<KIND>_<amount>__ or __REWARD_<KIND>_TEXT_<amount>__")
            continue
        spec = rewards.get(m["kind"])
        if spec is None:
            problems.append(f"__REWARD_{name}__: no reward kind '{m['kind']}' in knobs.yml")
            continue
        amount = reward_amount(m["amount"], spec["multiplier"])
        if amount < 1:
            problems.append(f"__REWARD_{name}__: {m['amount']} * {spec['multiplier']} rounds to {amount}")
            continue
        if m["text"]:
            values[name] = FIELD_RE.sub(lambda f: str(amount) if f[1] == "amount" else style[f[1]], spec["text"])
        else:
            values[name] = str(amount)
    fail_if(problems, "Unknown or invalid knob placeholders:")
    return values
