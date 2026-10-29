"""Shop prices from Minecraft/shop/items.yml, the vanilla lock and the `shop` knobs.

Every item has a value V: the fair price between players, per item. Base items get V in items.yml,
grouped by sell class; every other item is priced from its cheapest vanilla recipe (ingredients'
values * craft_factor). The shop sells above V and buys far below it:

    buy  = V * markup * rarity * multiplier          (per bundle, rounded up)
    sell = V * sell_ratio[class] * multiplier        (per bundle, rounded down)

A crafted item sells for exactly what its ingredients would (no craft_factor), so turning farm
output into blocks, tools or food never escapes the farm's sell ratio.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .. import config
from ..config import load_yaml
from ..errors import McDeployError, fail_if
from ..knobs import load_knobs

CLASSES = ("auto", "farm", "mined", "none")
RARITIES = ("common", "uncommon", "rare", "epic")
# Hard limits behind the knobs: the shop must stay worse than any player trade.
MIN_MARKUP = Decimal("1.5")
MAX_SELL_RATIO = Decimal("0.35")
MIN_SPREAD = Decimal(3)  # buy >= 3 * sell
BUNDLES = (1, 2, 4, 8, 16, 32, 64)
# Smallest bundle whose prices reach these, so whole-Moneten rounding stays small (<= 25% / 12.5%).
MIN_BUNDLE_SELL = 4
MIN_BUNDLE_BUY = 8
ESG_EXTRA_KEYS = {"spawnertype", "potiontypes", "enchantments", "instrument"}
ID_RE = re.compile(r"^[a-z0-9_]+$")
MAX_ROUNDS = 64


@dataclass(frozen=True)
class ShopKnobs:
    multiplier: Decimal
    markup: Decimal
    craft_factor: Decimal
    rarity: dict
    sell_ratio: dict


@dataclass
class Price:
    key: str                  # ESG item id: the vanilla item, or a custom entry's name
    material: str             # vanilla item id
    value: Decimal            # V per item
    cls: str                  # sell class, or "crafted"
    sell_unit: Decimal        # what the shop pays per item, before rounding
    rarity: str
    stack: int = 1            # ESG stack-size: prices are per bundle of this many
    buy: int = -1             # per bundle; -1 = not for sale
    sell: int = -1            # per bundle; -1 = the shop does not buy it
    offered: bool = True
    recipe: dict = field(default_factory=dict)  # chosen ingredient item -> amount per crafted item
    extra: dict = field(default_factory=dict)   # ESG keys of custom entries (spawnertype, ...)

    @property
    def buy_unit(self) -> Decimal:
        return Decimal(self.buy) / self.stack

    @property
    def sell_paid(self) -> Decimal:
        return Decimal(self.sell) / self.stack if self.sell >= 0 else Decimal(0)


def shop_dir():
    return config.MC_DIR / "shop"


def _dec(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def load_shop_knobs(data=None) -> ShopKnobs:
    raw = (load_knobs(data) if data is not None else load_knobs()).get("shop")
    if not isinstance(raw, dict):
        raise McDeployError("knobs.yml: 'shop' section is missing")
    problems: list[str] = []
    unknown = set(raw) - {"multiplier", "markup", "craft_factor", "rarity", "sell_ratio"}
    if unknown:
        problems.append(f"shop: unknown keys {sorted(unknown)}")
    nums = {k: _dec(raw.get(k)) for k in ("multiplier", "markup", "craft_factor")}
    for k, v in nums.items():
        if v is None or v <= 0:
            problems.append(f"shop.{k} must be a number > 0")
    if nums["markup"] is not None and nums["markup"] < MIN_MARKUP:
        problems.append(f"shop.markup must be >= {MIN_MARKUP} (the shop must sell above player prices)")
    if nums["craft_factor"] is not None and nums["craft_factor"] < 1:
        problems.append("shop.craft_factor must be >= 1 (otherwise crafting loops make items cheaper)")
    rarity = raw.get("rarity") if isinstance(raw.get("rarity"), dict) else {}
    ratio = raw.get("sell_ratio") if isinstance(raw.get("sell_ratio"), dict) else {}
    if set(rarity) != set(RARITIES):
        problems.append(f"shop.rarity must list exactly {list(RARITIES)}")
    if set(ratio) != set(CLASSES):
        problems.append(f"shop.sell_ratio must list exactly {list(CLASSES)}")
    rarity = {k: _dec(v) for k, v in rarity.items()}
    ratio = {k: _dec(v) for k, v in ratio.items()}
    for k, v in rarity.items():
        if v is None or v < 1:
            problems.append(f"shop.rarity.{k} must be a number >= 1")
    for k, v in ratio.items():
        if v is None or not Decimal(0) <= v <= MAX_SELL_RATIO:
            problems.append(f"shop.sell_ratio.{k} must be between 0 and {MAX_SELL_RATIO}")
    fail_if(problems, "knobs.yml shop section is invalid:")
    return ShopKnobs(nums["multiplier"], nums["markup"], nums["craft_factor"], rarity, ratio)


def _globs(data: dict, key: str, problems: list[str]) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(g, str) and g for g in value):
        problems.append(f"items.yml: '{key}' must be a list of item id globs")
        return []
    return value


def _matches(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


@dataclass
class ItemSource:
    values: dict          # item -> (V, class) for base items
    custom: dict          # custom key -> (material, V, class, extra)
    exclude: list[str]
    held_back: list[str]


def load_items(lock: dict, data=None) -> ItemSource:
    data = load_yaml(shop_dir() / "items.yml") if data is None else data
    if not isinstance(data, dict):
        raise McDeployError("Minecraft/shop/items.yml must be a mapping")
    problems: list[str] = []
    unknown = set(data) - {"exclude", "held_back", "values", "custom"}
    if unknown:
        problems.append(f"items.yml: unknown top-level keys {sorted(unknown)}")
    exclude = _globs(data, "exclude", problems)
    held_back = _globs(data, "held_back", problems)
    items = lock["items"]
    values: dict[str, tuple[Decimal, str]] = {}
    groups = data.get("values") or {}
    if not isinstance(groups, dict) or set(groups) - set(CLASSES):
        problems.append(f"items.yml: 'values' must map sell classes {list(CLASSES)} to item: value")
        groups = {}
    globbed: list[tuple[str, Decimal, str]] = []
    for cls, entries in groups.items():
        if not isinstance(entries, dict):
            problems.append(f"items.yml: values.{cls} must map item ids to values")
            continue
        for name, raw in entries.items():
            v = _dec(raw)
            if v is None or v <= 0:
                problems.append(f"items.yml: values.{cls}.{name}: value must be a number > 0")
            elif "*" in name:
                globbed.append((name, v, cls))
            elif name not in items:
                problems.append(f"items.yml: values.{cls}.{name}: not a {lock.get('version', '')} item (see vanilla.lock.yml)")
            elif name in values:
                problems.append(f"items.yml: {name} is listed under both {values[name][1]} and {cls}")
            elif _matches(name, exclude):
                problems.append(f"items.yml: {name} has a value but is excluded")
            else:
                values[name] = (v, cls)
    # Globs ("*_concrete": 3) fill in items not named exactly; the most specific glob wins.
    globbed.sort(key=lambda g: -len(g[0].replace("*", "")))
    for glob, v, cls in globbed:
        hits = [n for n in items if fnmatch.fnmatchcase(n, glob) and not _matches(n, exclude)]
        if not hits:
            problems.append(f"items.yml: values.{cls}.{glob} matches no item")
        for name in hits:
            values.setdefault(name, (v, cls))
    custom: dict[str, tuple] = {}
    raw_custom = data.get("custom") or {}
    if not isinstance(raw_custom, dict):
        problems.append("items.yml: 'custom' must be a mapping")
        raw_custom = {}
    for key, spec in raw_custom.items():
        if not isinstance(key, str) or not ID_RE.match(key) or key in items:
            problems.append(f"items.yml: custom {key!r}: needs a lowercase id that is not a vanilla item")
            continue
        if not isinstance(spec, dict):
            problems.append(f"items.yml: custom {key}: must be a mapping")
            continue
        bad = set(spec) - {"material", "value", "class", "esg"}
        v = _dec(spec.get("value"))
        extra = spec.get("esg") or {}
        if bad:
            problems.append(f"items.yml: custom {key}: unknown keys {sorted(bad)}")
        elif spec.get("material") not in items:
            problems.append(f"items.yml: custom {key}: material must be a vanilla item id")
        elif v is None or v <= 0:
            problems.append(f"items.yml: custom {key}: value must be a number > 0")
        elif spec.get("class") not in CLASSES:
            problems.append(f"items.yml: custom {key}: class must be one of {list(CLASSES)}")
        elif not isinstance(extra, dict) or set(extra) - ESG_EXTRA_KEYS:
            problems.append(f"items.yml: custom {key}: esg may only set {sorted(ESG_EXTRA_KEYS)}")
        else:
            custom[key] = (spec["material"], v, spec["class"], dict(extra))
    fail_if(problems, "Minecraft/shop/items.yml is invalid:")
    return ItemSource(values, custom, exclude, held_back)


def _alternatives(key: str, lock: dict) -> list[str]:
    if key.startswith("#"):
        return list(lock["tags"].get(key[1:], []))
    return key.split("|")


def derive(lock: dict, source: ItemSource, knobs: ShopKnobs) -> dict[str, Price]:
    """Value and sell price of every priceable item (offered or not), before bundling."""
    items = lock["items"]
    prices: dict[str, Price] = {}
    for name, (v, cls) in source.values.items():
        prices[name] = Price(name, name, v, cls, v * knobs.sell_ratio[cls], items[name]["rarity"])

    def cheapest(key: str) -> tuple[str, Price] | None:
        found = [(alt, prices[alt]) for alt in _alternatives(key, lock) if alt in prices]
        return min(found, key=lambda f: (f[1].value, f[1].sell_unit, f[0])) if found else None

    derivable = [n for n in lock["recipes"] if n in items and n not in source.values
                 and not _matches(n, source.exclude)]
    for _ in range(MAX_ROUNDS):
        changed = False
        for name in derivable:
            best = None
            for recipe in lock["recipes"][name]:
                parts = []
                for key, qty in recipe["in"].items():
                    pick = cheapest(key)
                    if pick is None:
                        break
                    parts.append((pick, qty))
                else:
                    out = Decimal(recipe["out"])
                    cost = sum((p.value * qty for (_, p), qty in parts), Decimal(0)) / out
                    sell = sum((p.sell_unit * qty for (_, p), qty in parts), Decimal(0)) / out
                    used = {alt: Decimal(qty) / out for (alt, _), qty in parts}
                    cand = (cost * knobs.craft_factor, sell, used)
                    if best is None or cand[:2] < best[:2]:
                        best = cand
            if best is None:
                continue
            old = prices.get(name)
            if old is None or best[0] < old.value:
                prices[name] = Price(name, name, best[0], "crafted", best[1], items[name]["rarity"], recipe=best[2])
                changed = True
        if not changed:
            break
    else:
        raise McDeployError("shop: recipe values did not settle (a recipe loop makes items cheaper every round)")
    for key, (material, v, cls, extra) in source.custom.items():
        prices[key] = Price(key, material, v, cls, v * knobs.sell_ratio[cls], items[material]["rarity"], extra=extra)
    # Held back items are priced (their products need a value) but not offered, and neither is
    # anything crafted from them: if the shop doesn't sell elytra, it doesn't sell what needs one.
    for p in prices.values():
        p.offered = not _matches(p.key, source.held_back)
    for _ in range(MAX_ROUNDS):
        changed = False
        for p in prices.values():
            if p.offered and p.recipe and not all(prices[i].offered for i in p.recipe):
                p.offered = False
                changed = True
        if not changed:
            break
    return prices


def bundle(p: Price, knobs: ShopKnobs, max_stack: int) -> None:
    """Choose the bundle size and round the prices (Moneten have no decimals)."""
    buy_unit = p.value * knobs.markup * knobs.rarity[p.rarity] * knobs.multiplier
    sell_unit = p.sell_unit * knobs.multiplier
    sizes = [s for s in BUNDLES if s <= max_stack] or [1]
    stack = sizes[-1]
    for s in sizes:
        if (sell_unit == 0 or sell_unit * s >= MIN_BUNDLE_SELL) and buy_unit * s >= MIN_BUNDLE_BUY:
            stack = s
            break
    p.stack = stack
    p.buy = int((buy_unit * stack).to_integral_value(rounding=ROUND_CEILING))
    sell = int((sell_unit * stack).to_integral_value(rounding=ROUND_FLOOR))
    p.sell = sell if sell >= 1 else -1


def price_all(lock: dict, source: ItemSource, knobs: ShopKnobs) -> dict[str, Price]:
    prices = derive(lock, source, knobs)
    for p in prices.values():
        bundle(p, knobs, lock["items"][p.material]["stack"])
    return prices


def check_prices(prices: dict[str, Price], lock: dict, knobs: ShopKnobs) -> list[str]:
    """The player-band guarantees and the no-arbitrage rule, on the rounded prices."""
    problems: list[str] = []
    for p in prices.values():
        if not p.offered:
            continue
        floor_buy = p.value * MIN_MARKUP * knobs.multiplier
        if p.buy_unit < floor_buy:
            problems.append(f"{p.key}: buys for {p.buy_unit:.2f}/item, below {MIN_MARKUP} * V = {floor_buy:.2f}")
        if p.sell >= 0:
            if p.sell_paid > p.value * MAX_SELL_RATIO * knobs.multiplier:
                problems.append(f"{p.key}: sells for {p.sell_paid:.2f}/item, above {MAX_SELL_RATIO} * V")
            if p.buy < MIN_SPREAD * p.sell:
                problems.append(f"{p.key}: buy {p.buy} is less than {MIN_SPREAD} * sell {p.sell}")
    # Buying the ingredients of any vanilla recipe and selling the result must lose money.
    for name, recipes in lock["recipes"].items():
        product = prices.get(name)
        if product is None or not product.offered or product.sell < 0:
            continue
        for recipe in recipes:
            cost = Decimal(0)
            for key, qty in recipe["in"].items():
                buys = [prices[a].buy_unit for a in _alternatives(key, lock)
                        if a in prices and prices[a].offered and prices[a].buy > 0]
                if not buys:
                    break
                cost += min(buys) * qty
            else:
                gain = product.sell_paid * recipe["out"]
                if gain > cost:
                    problems.append(
                        f"{name}: crafting {recipe['out']} from shop-bought {dict(recipe['in'])} costs "
                        f"{cost:.2f} but sells for {gain:.2f} (arbitrage)")
    return problems
