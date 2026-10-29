"""SmartSpawner income at shop prices: what one spawner earns per hour while a player is in range.

SmartSpawner sells its loot through EconomyShopGUI (sell_integration.yml, SHOP_ONLY): per item it
pays the generated shop's sell price / stack-size. A spawner runs 3600/delay cycles an hour with
min_mobs..max_mobs mobs each, so per spawner:

    income/h = 3600 / delay * (min_mobs + max_mobs) / 2 * sum(chance/100 * avg(amount) * unit_sell)

Lint fails when any spawner type earns more than the `shop.spawner_income_cap` knob. A stack of
N spawners earns N times as much. Items the shop does not buy (held back, sell -1) earn nothing:
SmartSpawner 1.9.0 only sells loot whose price is > 0 (SpawnerData).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .. import config
from ..config import load_yaml
from ..errors import McDeployError
from .pricing import Price, ShopKnobs

PLUGIN = Path("Backend") / "plugins" / "SmartSpawner"
TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000}
TIME_RE = re.compile(r"(\d+)(mo|[smhdwy])")
AMOUNT_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def plugin_dir() -> Path:
    return config.MC_DIR / PLUGIN


def parse_time(text) -> int:
    """SmartSpawner's time format in seconds: 25s, 5m, 1d_2h_30m_15s."""
    raw = str(text).strip().lower()
    parts = raw.split("_")
    matches = [TIME_RE.fullmatch(p) for p in parts]
    if not raw or not all(matches):
        raise McDeployError(f"not a SmartSpawner time: {text!r} (use e.g. 25s, 5m or 1h_30m)")
    seconds = sum(int(m[1]) * TIME_UNITS[m[2]] for m in matches)
    if seconds <= 0:
        raise McDeployError(f"SmartSpawner time must be > 0: {text!r}")
    return seconds


def material(item: str) -> str:
    """Vanilla item id of a loot entry; 'tipped_arrow[potion_contents=...]' counts as tipped_arrow."""
    return item.split("[", 1)[0].strip().lower().removeprefix("minecraft:")


def _amount(value) -> Decimal:
    m = AMOUNT_RE.match(str(value).strip())
    if not m:
        raise McDeployError(f"loot amount must be N or MIN-MAX, got {value!r}")
    low = int(m[1])
    high = int(m[2]) if m[2] is not None else low
    if high < low:
        raise McDeployError(f"loot amount {value!r}: maximum below minimum")
    return Decimal(low + high) / 2


@dataclass(frozen=True)
class Rate:
    mobs_per_hour: Decimal
    max_stack: int


@dataclass
class Income:
    name: str                # spawner_mobs.yml / spawner_items.yml key
    per_hour: Decimal        # one spawner, Moneten
    top_item: str            # the loot item that earns the most ("" when it earns nothing)


def load_rate(data: dict) -> Rate:
    props = ((data or {}).get("spawner_properties") or {}).get("default")
    if not isinstance(props, dict):
        raise McDeployError("SmartSpawner config.yml: spawner_properties.default is missing")
    try:
        delay = parse_time(props.get("delay"))
        low, high, stack = (int(props[k]) for k in ("min_mobs", "max_mobs", "max_stack_size"))
    except (KeyError, TypeError, ValueError) as exc:
        raise McDeployError(f"SmartSpawner config.yml: spawner_properties.default needs min_mobs, max_mobs "
                            f"and max_stack_size as numbers ({exc})") from exc
    if not 0 <= low <= high or stack < 1:
        raise McDeployError("SmartSpawner config.yml: need 0 <= min_mobs <= max_mobs and max_stack_size >= 1")
    return Rate(Decimal(3600) / delay * Decimal(low + high) / 2, stack)


def unit_sell(prices: dict[str, Price]) -> dict[str, Decimal]:
    """What the shop pays per item for each vanilla item (ESG's sell / stack-size). Missing = 0."""
    return {p.material: p.sell_paid for p in prices.values() if p.offered and p.key == p.material}


def income(name: str, entry, rate: Rate, paid: dict[str, Decimal]) -> Income:
    loot = (entry.get("loot") or {}) if isinstance(entry, dict) else {}
    if not isinstance(loot, dict):
        raise McDeployError(f"{name}: loot must be a mapping")
    per_mob: dict[str, Decimal] = {}
    for slot, drop in loot.items():
        if not isinstance(drop, dict) or not isinstance(drop.get("item"), str):
            raise McDeployError(f"{name}: loot {slot} needs an item")
        mat = material(drop["item"])
        try:
            chance = Decimal(str(drop.get("chance", 100.0))) / 100
        except ArithmeticError as exc:
            raise McDeployError(f"{name}: loot {slot} chance is not a number") from exc
        per_mob[mat] = per_mob.get(mat, Decimal(0)) + chance * _amount(drop.get("amount", 1)) * paid.get(mat, Decimal(0))
    top = max(per_mob, key=lambda m: per_mob[m]) if any(per_mob.values()) else ""
    return Income(name, sum(per_mob.values(), Decimal(0)) * rate.mobs_per_hour, top)


def _load(name: str) -> dict:
    path = plugin_dir() / name
    data = load_yaml(path) if path.is_file() else None
    if not isinstance(data, dict):
        raise McDeployError(f"{PLUGIN.as_posix()}/{name}: missing or not a mapping")
    return data


def incomes(prices: dict[str, Price]) -> tuple[Rate, list[Income]]:
    rate = load_rate(_load("config.yml"))
    paid = unit_sell(prices)
    mobs = _load("spawner_mobs.yml")
    out = [income(n, e, rate, paid) for n, e in mobs.items() if n != "custom_spawners"]
    out += [income(f"custom:{n}", e, rate, paid) for n, e in (mobs.get("custom_spawners") or {}).items()]
    items = _load("spawner_items.yml")
    out += [income(f"item:{n}", e, rate, paid) for n, e in items.items() if n != "custom_spawners"]
    return rate, out


def check(prices: dict[str, Price], knobs: ShopKnobs) -> list[str]:
    if not plugin_dir().is_dir():
        return []
    problems: list[str] = []
    try:
        sell = _load("sell_integration.yml")
        items = _load("spawner_items.yml")
        _, found = incomes(prices)
    except McDeployError as exc:
        return [f"SmartSpawner: {exc}"]
    # Anything but the generated shop as price source bypasses the anti-farm sell classes.
    if sell.get("price_source_mode") != "SHOP_ONLY":
        problems.append("SmartSpawner sell_integration.yml: price_source_mode must be SHOP_ONLY "
                        "(other modes fall back to SmartSpawner's own prices when the shop pays 0)")
    if (sell.get("custom_prices") or {}).get("enabled") is not False:
        problems.append("SmartSpawner sell_integration.yml: custom_prices.enabled must be false")
    if (sell.get("shop_integration") or {}).get("enabled") is not True or sell.get("enabled") is not True:
        problems.append("SmartSpawner sell_integration.yml: enabled and shop_integration.enabled must be true")
    spawners = sorted(n for n in items if n != "custom_spawners") + sorted(items.get("custom_spawners") or {})
    if spawners:
        problems.append(f"SmartSpawner spawner_items.yml: item spawners make items out of nothing, remove "
                        f"{', '.join(spawners)}")
    for inc in found:
        if inc.per_hour > knobs.spawner_income_cap:
            problems.append(f"SmartSpawner {inc.name}: earns {inc.per_hour:.1f} Moneten/h per spawner, over "
                            f"shop.spawner_income_cap {knobs.spawner_income_cap} (mostly {inc.top_item}); trim "
                            f"its loot in spawner_mobs.yml")
    return problems


def report(prices: dict[str, Price], knobs: ShopKnobs) -> list[str]:
    if not plugin_dir().is_dir():
        return []
    rate, found = incomes(prices)
    out = ["## SmartSpawner income", "",
           f"{rate.mobs_per_hour:.0f} mobs/h per spawner, stacks up to {rate.max_stack}. "
           f"Cap: {knobs.spawner_income_cap} Moneten/h per spawner.", "",
           "| Spawner | Moneten/h | Full stack/h | Top item |", "|---|---:|---:|---|"]
    out += [f"| {i.name} | {i.per_hour:.1f} | {i.per_hour * rate.max_stack:.0f} | {i.top_item or '-'} |"
            for i in sorted(found, key=lambda i: (-i.per_hour, i.name)) if i.per_hour > 0]
    idle = sorted(i.name for i in found if i.per_hour == 0)
    out += ["", f"Earn nothing: {', '.join(idle) or 'none'}", ""]
    by_type = {i.name: i.per_hour for i in found}
    sold = [p for p in prices.values() if p.offered and p.material == "spawner" and "spawnertype" in p.extra]
    if sold:
        out += ["### Spawners in the shop", "", "| Entry | Buy | Moneten/h | Payback (h) |", "|---|---:|---:|---:|"]
        for p in sorted(sold, key=lambda p: p.key):
            earns = by_type.get(str(p.extra["spawnertype"]).lower(), Decimal(0))
            payback = f"{p.buy_unit / earns:.0f}" if earns > 0 else "never"
            out.append(f"| {p.key} | {p.buy} | {earns:.1f} | {payback} |")
        out.append("")
    return out
