"""Shop menu layout (Minecraft/shop/layout.yml) and the EconomyShopGUI files it becomes.

Main menu -> category -> (optional) sub-sections -> pages of items. Each offered item lands in the
first leaf whose `match` globs fit its id; the one `rest: true` leaf takes everything left over.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field

from ..config import load_yaml
from ..errors import McDeployError, fail_if
from .pricing import Price, shop_dir

ESG = "plugins/EconomyShopGUI"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PER_PAGE = 28  # the 7x4 item area of ITEM_LAYOUT
ITEM_LAYOUT = ["0 0 0 0 0 0 0 0 0"] + ["0 1 1 1 1 1 1 1 0"] * 4 + ["0 0 0 0 0 0 0 0 0"]
MAIN_MENU_SLOTS = 45   # 5 rows above the main menu's navigation bar
MAX_SUBS = 5           # one centred row of the 3-row category menu
FILL = {"material": "GRAY_STAINED_GLASS_PANE", "displayname": " "}


@dataclass
class Leaf:
    name: str
    title: str
    color: str
    icon: str
    match: list[str]
    rest: bool
    items: list[Price] = field(default_factory=list)


@dataclass
class Category:
    name: str
    title: str
    color: str
    icon: str
    slot: int
    leaf: Leaf | None            # a category without sub-sections is itself a leaf
    subs: list[Leaf] = field(default_factory=list)


@dataclass
class Layout:
    categories: list[Category]
    retired: list[str]

    def leaves(self) -> list[Leaf]:
        return [l for c in self.categories for l in ([c.leaf] if c.leaf else c.subs)]


def _leaf(spec: dict, where: str, items: dict, problems: list[str]) -> Leaf | None:
    unknown = set(spec) - {"name", "title", "color", "icon", "match", "rest", "slot", "subs"}
    if unknown:
        problems.append(f"{where}: unknown keys {sorted(unknown)}")
    name, title, color, icon = spec.get("name"), spec.get("title"), spec.get("color", "&f"), spec.get("icon")
    match = spec.get("match") or []
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append(f"{where}: name must match {NAME_RE.pattern}")
        return None
    if not isinstance(title, str) or not title or not isinstance(color, str):
        problems.append(f"{name}: title (and color) must be strings")
    if icon not in items:
        problems.append(f"{name}: icon must be a vanilla item id")
    if not isinstance(match, list) or not all(isinstance(m, str) and m for m in match):
        problems.append(f"{name}: match must be a list of item id globs")
        match = []
    rest = spec.get("rest", False) is True
    if not match and not rest and "subs" not in spec:
        problems.append(f"{name}: needs match globs (or rest: true)")
    return Leaf(name, title or name, color, icon or "stone", match, rest)


def load_layout(lock: dict, data=None) -> Layout:
    data = load_yaml(shop_dir() / "layout.yml") if data is None else data
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise McDeployError("Minecraft/shop/layout.yml needs a 'categories' list")
    problems: list[str] = []
    unknown = set(data) - {"categories", "retired"}
    if unknown:
        problems.append(f"layout.yml: unknown top-level keys {sorted(unknown)}")
    items = lock["items"]
    categories: list[Category] = []
    for i, spec in enumerate(data["categories"]):
        if not isinstance(spec, dict):
            problems.append(f"layout.yml: categories[{i}] must be a mapping")
            continue
        head = _leaf(spec, f"categories[{i}]", items, problems)
        slot = spec.get("slot")
        if head is None:
            continue
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < MAIN_MENU_SLOTS:
            problems.append(f"{head.name}: slot must be a main menu slot 0-{MAIN_MENU_SLOTS - 1}")
            slot = 0
        subs_raw = spec.get("subs")
        if subs_raw is None:
            categories.append(Category(head.name, head.title, head.color, head.icon, slot, head))
            continue
        if head.match or head.rest:
            problems.append(f"{head.name}: a category with subs gets its items from the subs only")
        if not isinstance(subs_raw, list) or not 1 <= len(subs_raw) <= MAX_SUBS:
            problems.append(f"{head.name}: subs must be a list of 1-{MAX_SUBS} sub-sections")
            subs_raw = []
        subs = []
        for j, sub in enumerate(subs_raw):
            if isinstance(sub, dict) and "subs" not in sub and "slot" not in sub:
                leaf = _leaf(sub, f"{head.name}.subs[{j}]", items, problems)
                if leaf:
                    subs.append(leaf)
            else:
                problems.append(f"{head.name}.subs[{j}]: must be a mapping without slot or subs")
        categories.append(Category(head.name, head.title, head.color, head.icon, slot, None, subs))
    layout = Layout(categories, data.get("retired") or [])
    names = [c.name for c in categories] + [l.name for c in categories for l in c.subs]
    problems += [f"layout.yml: section name '{n}' is used twice" for n in sorted({n for n in names if names.count(n) > 1})]
    slots = [c.slot for c in categories]
    problems += [f"layout.yml: main menu slot {s} is used twice" for s in sorted({s for s in slots if slots.count(s) > 1})]
    if sum(l.rest for l in layout.leaves()) != 1:
        problems.append("layout.yml: exactly one leaf needs rest: true (it takes every item no match covers)")
    if not isinstance(layout.retired, list) or not all(isinstance(r, str) and r.endswith(".yml") for r in layout.retired):
        problems.append("layout.yml: retired must be a list of section file paths like Building/stone.yml")
    else:
        # ESG keys sections by file name alone (ConfigManager.loadSections ignores folders), so a
        # disabled Mobs/spawners.yml and our spawners.yml are the same section and one hides the other.
        own = {f"{c.name}.yml" for c in categories} | {f"{c.name.capitalize()}/{l.name}.yml" for c in categories for l in c.subs}
        problems += [f"layout.yml: section name '{r.rsplit('/', 1)[-1][:-4]}' clashes with retired {r}: EconomyShopGUI "
                     f"ignores folders, so the disabled file hides ours; pick another name"
                     for r in layout.retired if r not in own and r.rsplit("/", 1)[-1][:-4] in names]
    fail_if(problems, "Minecraft/shop/layout.yml is invalid:")
    return layout


def place(layout: Layout, prices: dict[str, Price]) -> None:
    """Put every offered item into its leaf, ordered by the matching glob, then by id."""
    leaves = layout.leaves()
    rest = next(l for l in leaves if l.rest)
    placed: dict[str, list[tuple[int, str, Price]]] = {l.name: [] for l in leaves}
    for p in prices.values():
        if not p.offered or (p.buy < 0 and p.sell < 0):
            continue
        for leaf in leaves:
            hit = next((i for i, g in enumerate(leaf.match) if fnmatch.fnmatchcase(p.key, g)), None)
            if hit is not None:
                placed[leaf.name].append((hit, p.key, p))
                break
        else:
            placed[rest.name].append((len(rest.match), p.key, p))
    for leaf in leaves:
        leaf.items = [p for _, _, p in sorted(placed[leaf.name], key=lambda t: t[:2])]


# ---------------------------------------------------------------- YAML text

def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def _yaml(value, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for key, v in value.items():
        if isinstance(v, dict):
            lines.append(f"{pad}{key}:" + (" {}" if not v else ""))
            lines += _yaml(v, indent + 1)
        elif isinstance(v, list):
            lines.append(f"{pad}{key}:" + (" []" if not v else ""))
            lines += [f"{pad}- {_scalar(x)}" for x in v]
        else:
            lines.append(f"{pad}{key}: {_scalar(v)}")
    return lines


def dump(doc: dict) -> str:
    return "# Generated from Minecraft/shop/ by mcdeploy at render time. Do not edit on the server.\n" \
        + "\n".join(_yaml(doc)) + "\n"


def _title(color: str, title: str) -> str:
    return f"&8<---- {color}<bold>{title} &8---->"


def _item_doc(p: Price) -> dict:
    doc: dict = {"material": p.material.upper(), **p.extra, "buy": p.buy, "sell": p.sell}
    if p.stack > 1:
        doc["stack-size"] = p.stack
    return doc


def _leaf_files(leaf: Leaf, path: str, slot: int, sub: bool) -> dict[str, str]:
    section: dict = {"enable": True, "title": _title(leaf.color, leaf.title), "slot": slot}
    if sub:
        section.update({"hidden": True, "sub-section": True})
    section.update({
        "item": {"displayname": f"{leaf.color}<bold>{leaf.title}", "material": leaf.icon.upper()},
        "fill-item": dict(FILL),
        "item-layout": list(ITEM_LAYOUT),
    })
    pages = {}
    for n in range(0, max(len(leaf.items), 1), PER_PAGE):
        chunk = leaf.items[n:n + PER_PAGE]
        pages[f"page{n // PER_PAGE + 1}"] = {"items": {p.key: _item_doc(p) for p in chunk}}
    return {f"{ESG}/sections/{path}": dump(section), f"{ESG}/shops/{path}": dump({"pages": pages})}


def sub_slots(n: int) -> list[int]:
    """Centred, one apart, in the middle row of a 3-row menu (the stock files' 11/13/15/17 for 4)."""
    return [14 - (n - 1) + 2 * i for i in range(n)]


def render_files(layout: Layout) -> dict[str, str]:
    files: dict[str, str] = {}
    used = {c.slot for c in layout.categories}
    # Hidden sub-sections still need a unique main menu slot; give them the unused ones.
    spare = iter(s for s in range(MAIN_MENU_SLOTS) if s not in used)
    for cat in layout.categories:
        if cat.leaf is not None:
            if cat.leaf.items:
                files.update(_leaf_files(cat.leaf, f"{cat.name}.yml", cat.slot, sub=False))
            continue
        subs = [s for s in cat.subs if s.items]
        if not subs:
            continue
        folder = cat.name.capitalize()
        hub_items = {}
        for leaf, slot in zip(subs, sub_slots(len(subs))):
            files.update(_leaf_files(leaf, f"{folder}/{leaf.name}.yml", next(spare), sub=True))
            hub_items[leaf.name] = {"material": leaf.icon.upper(), "section": leaf.name,
                                    "displayname": f"{leaf.color}<bold>{leaf.title}", "slot": slot}
        section = {
            "enable": True, "title": _title(cat.color, cat.title), "slot": cat.slot,
            "item": {"displayname": f"{cat.color}<bold>{cat.title}", "material": cat.icon.upper()},
            "fill-item": dict(FILL),
            "nav-bar": {"mode": "SELF", "fill-item": dict(FILL), "items": {
                "back": {"material": "ARROW", "name": "%translations-back%", "action": "BACK", "slot": 9}}},
        }
        files[f"{ESG}/sections/{cat.name}.yml"] = dump(section)
        files[f"{ESG}/shops/{cat.name}.yml"] = dump({"pages": {"page1": {"gui-rows": 3, "items": hub_items}}})
    # Deploys never delete server files: switch off the plugin's stock sections we don't use.
    for path in layout.retired:
        if f"{ESG}/sections/{path}" not in files:
            files[f"{ESG}/sections/{path}"] = dump({"enable": False})
            files[f"{ESG}/shops/{path}"] = dump({"pages": {}})
    return files
