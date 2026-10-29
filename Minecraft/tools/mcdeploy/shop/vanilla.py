"""Vanilla item facts for the shop: every item's rarity and max stack size, the item tags, and the
recipes that turn items into other items. `mc shop-audit` reads them from the server jar (Mojang's
data generator) and writes Minecraft/shop/vanilla.lock.yml, so rendering never needs the jar.

The lock holds only the ingredient amounts per recipe (no patterns, groups or other recipe data).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from .. import config
from ..config import Target, load_yaml
from ..errors import McDeployError

LOCK_NAME = "vanilla.lock.yml"
RARITIES = ("common", "uncommon", "rare", "epic")
COOKING = {"minecraft:smelting", "minecraft:blasting", "minecraft:smoking", "minecraft:campfire_cooking"}


def lock_path() -> Path:
    return config.MC_DIR / "shop" / LOCK_NAME


def _id(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("minecraft:") else value


def _ingredient(value) -> str | None:
    """Recipe ingredient -> lock key: "item", "#tag" or "a|b" (any one of several items)."""
    if isinstance(value, str):
        return "#" + _id(value[1:]) if value.startswith("#") else _id(value)
    if isinstance(value, list) and value and all(isinstance(v, str) and not v.startswith("#") for v in value):
        return "|".join(sorted(_id(v) for v in value))
    return None


def _add(counts: dict[str, int], key: str | None, amount: int = 1) -> bool:
    if key is None:
        return False
    counts[key] = counts.get(key, 0) + amount
    return True


def _recipe_inputs(recipe: dict) -> dict[str, int] | None:
    """Ingredient key -> amount consumed, or None for recipes the shop does not price from."""
    kind = recipe.get("type")
    counts: dict[str, int] = {}
    if kind == "minecraft:crafting_shaped":
        key = recipe["key"]
        for row in recipe["pattern"]:
            for ch in row:
                if ch != " " and not _add(counts, _ingredient(key[ch])):
                    return None
    elif kind == "minecraft:crafting_shapeless":
        for ing in recipe["ingredients"]:
            if not _add(counts, _ingredient(ing)):
                return None
    elif kind == "minecraft:stonecutting" or kind in COOKING:
        if not _add(counts, _ingredient(recipe["ingredient"])):
            return None
    elif kind == "minecraft:smithing_transform":
        for part in ("template", "base", "addition"):
            if not _add(counts, _ingredient(recipe[part])):
                return None
    elif kind == "minecraft:crafting_transmute":
        if not (_add(counts, _ingredient(recipe["input"]))
                and _add(counts, _ingredient(recipe["material"]), int(recipe.get("material_count", 1)))):
            return None
    else:
        return None  # special recipes (fireworks, banners, maps, trims, brewing...)
    return counts


def _tags(tag_dir: Path) -> dict[str, list[str]]:
    raw: dict[str, list[str]] = {}
    for path in sorted(tag_dir.rglob("*.json")):
        name = path.relative_to(tag_dir).with_suffix("").as_posix()
        values = json.loads(path.read_text(encoding="utf-8")).get("values", [])
        raw[name] = [v["id"] if isinstance(v, dict) else v for v in values]

    def members(name: str, seen: frozenset[str]) -> set[str]:
        out: set[str] = set()
        for v in raw.get(name, []):
            if v.startswith("#"):
                inner = _id(v[1:])
                if inner not in seen:
                    out |= members(inner, seen | {inner})
            else:
                out.add(_id(v))
        return out

    return {name: sorted(members(name, frozenset({name}))) for name in raw}


def build_lock(generated: Path, version: str) -> dict:
    """Lock data from the output folder of `net.minecraft.data.Main --reports --server`."""
    item_dir = generated / "reports" / "minecraft" / "components" / "item"
    data_dir = generated / "data" / "minecraft"
    if not item_dir.is_dir() or not (data_dir / "recipe").is_dir():
        raise McDeployError(f"data generator output in {generated} has no item reports or recipes")
    items: dict[str, dict] = {}
    for path in sorted(item_dir.glob("*.json")):
        comps = json.loads(path.read_text(encoding="utf-8"))["components"]
        items[path.stem] = {
            "rarity": comps.get("minecraft:rarity", "common"),
            "stack": int(comps.get("minecraft:max_stack_size", 64)),
        }
    tags = _tags(data_dir / "tags" / "item")
    recipes: dict[str, list[dict]] = {}
    for path in sorted((data_dir / "recipe").rglob("*.json")):
        recipe = json.loads(path.read_text(encoding="utf-8"))
        result = recipe.get("result")
        if not isinstance(result, dict) or "id" not in result:
            continue
        inputs = _recipe_inputs(recipe)
        if not inputs:
            continue
        entry = {"out": int(result.get("count", 1)), "in": dict(sorted(inputs.items()))}
        target_list = recipes.setdefault(_id(result["id"]), [])
        if entry not in target_list:
            target_list.append(entry)
    used = {k[1:] for rs in recipes.values() for r in rs for k in r["in"] if k.startswith("#")}
    return {
        "version": version,
        "items": items,
        "tags": {name: tags[name] for name in sorted(used) if name in tags},
        "recipes": dict(sorted(recipes.items())),
    }


def dump_lock(lock: dict) -> str:
    """One JSON flow mapping per line: valid YAML, small diffs, no YAML library layout questions."""
    j = lambda v: json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
    lines = [
        "# Generated by `mc shop-audit` from the server jar. Do not edit: vanilla facts the shop prices",
        "# are derived from (rarity, max stack size, item tags, ingredient amounts per recipe).",
        f"version: {j(lock['version'])}",
        "items:",
        *(f"  {name}: {j(spec)}" for name, spec in lock["items"].items()),
        "tags:",
        *(f"  {name}: {j(members)}" for name, members in lock["tags"].items()),
        "recipes:",
    ]
    for name, entries in lock["recipes"].items():
        lines.append(f"  {name}:")
        lines += [f"  - {j(e)}" for e in entries]
    return "\n".join(lines) + "\n"


def load_lock() -> dict:
    data = load_yaml(lock_path()) or {}
    if not isinstance(data.get("items"), dict) or not isinstance(data.get("recipes"), dict):
        raise McDeployError(f"Minecraft/shop/{LOCK_NAME} is invalid; regenerate it with `mc shop-audit`")
    data.setdefault("tags", {})
    return data


def generate_from_jar(target: Target) -> tuple[dict, str]:
    """Run Mojang's data generator from the target's cached server jar in its Java image."""
    from ..boottest import VERSION_CACHE, docker, jar_cache_dir

    jar_dir = jar_cache_dir(target)
    if not (jar_dir / "server.jar").is_file():
        raise McDeployError(f"{target.name}: no cached server jar in {jar_dir}; run with --jars-from remote first")
    name = f"mcd-shop-audit-{uuid.uuid4().hex[:8]}"
    # Paperclip downloads the Mojang jar and stops (patchonly); the bundled vanilla server then
    # runs the data generator instead of the game.
    script = (
        "java -Dpaperclip.patchonly=true -jar server.jar >/dev/null && "
        "java -DbundlerMainClass=net.minecraft.data.Main -jar cache/mojang_*.jar --reports --server --output /work/out >/dev/null"
    )
    work = Path(tempfile.mkdtemp(prefix="mcd-audit-"))
    try:
        docker("create", "--name", name, "-w", "/work", target.java_image, "sh", "-c", script, timeout=600)
        docker("cp", str(jar_dir / "server.jar"), f"{name}:/work/server.jar", timeout=600)
        result = docker("start", "-a", name, timeout=900, check=False)
        exit_code = docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip()
        if exit_code != "0":
            raise McDeployError(f"data generator failed (exit {exit_code}): {(result.stderr or result.stdout).strip()[-2000:]}")
        docker("cp", f"{name}:/work/out", str(work), timeout=600)
        version_file = jar_dir / VERSION_CACHE
        version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else "unknown"
        return build_lock(work / "out", version), version
    finally:
        docker("rm", "-f", name, check=False)
        shutil.rmtree(work, ignore_errors=True)
