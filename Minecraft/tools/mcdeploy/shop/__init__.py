"""EconomyShopGUI admin shop, generated at render time from Minecraft/shop/ and the `shop` knobs.

The plugin's sections/ and shops/ files are never stored in the repo: generated_files() builds them
whenever a backend is rendered (deploy, boot test, mc render), and lint checks the price rules.
"""
from __future__ import annotations

import hashlib

from .. import config
from ..config import Target
from ..errors import McDeployError
from ..formats import parse
from .layout import load_layout, place, render_files
from .pricing import Price, _matches, check_prices, load_items, load_shop_knobs, price_all, shop_dir
from .vanilla import LOCK_NAME, load_lock

PROFILE = "backend"


def build() -> tuple[dict[str, str], list, dict[str, Price], list[str]]:
    """(files, layout leaves, prices, rule problems) from the committed source."""
    lock = load_lock()
    knobs = load_shop_knobs()
    prices = price_all(lock, load_items(lock), knobs)
    layout = load_layout(lock)
    place(layout, prices)
    return render_files(layout), layout.leaves(), prices, check_prices(prices, lock, knobs)


_cache: dict[tuple, dict[str, str]] = {}


def _inputs() -> tuple:
    paths = [shop_dir() / n for n in ("items.yml", "layout.yml", LOCK_NAME)] + [config.KNOBS_FILE]
    return tuple((str(p), hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None) for p in paths)


def generated_files(target: Target) -> dict[str, str]:
    """Source-relative path -> file text the target gets on top of its repo files."""
    if target.profile != PROFILE or not (shop_dir() / "items.yml").is_file():
        return {}
    key = _inputs()  # one deploy asks several times; building takes about a second
    if key not in _cache:
        files, _, _, problems = build()
        if problems:
            raise McDeployError("shop prices break the shop rules (run mc lint):\n" + "\n".join(f"  - {p}" for p in problems))
        _cache.clear()
        _cache[key] = files
    return dict(_cache[key])


def check() -> list[str]:
    """Lint: the source is valid, the prices keep the rules, and every generated file parses."""
    if not (shop_dir() / "items.yml").is_file():
        return []
    try:
        files, _, _, problems = build()
    except McDeployError as exc:
        return [str(exc)]
    for rel, text in files.items():
        try:
            parse("yaml", text)
        except McDeployError as exc:
            problems.append(f"generated {rel}: {exc}")
    return [f"shop: {p}" for p in problems]


def unpriced() -> list[str]:
    """Vanilla items that are neither excluded nor priced (no value and no priceable recipe)."""
    lock = load_lock()
    source = load_items(lock)
    prices = price_all(lock, source, load_shop_knobs())
    return sorted(n for n in lock["items"] if n not in prices and not _matches(n, source.exclude))


def report() -> str:
    """Markdown price table per shop leaf, for reviewing a price change."""
    _, leaves, prices, problems = build()
    out = ["# Shop prices", "", "Prices per bundle (Moneten). V = player price per item. "
           "Sell -1 = the shop does not buy it.", ""]
    for leaf in leaves:
        if not leaf.items:
            continue
        out += [f"## {leaf.title} ({leaf.name}, {len(leaf.items)} items)", "",
                "| Item | Class | V | Bundle | Buy | Sell |", "|---|---|---:|---:|---:|---:|"]
        out += [f"| {p.key} | {p.cls} | {p.value:.2f} | {p.stack} | {p.buy} | {p.sell} |" for p in leaf.items]
        out.append("")
    held = sorted(p.key for p in prices.values() if not p.offered)
    out += ["## Priced but held back (not in the shop)", "", ", ".join(held) or "none", ""]
    if problems:
        out += ["## Rule problems", "", *[f"- {p}" for p in problems], ""]
    return "\n".join(out)
