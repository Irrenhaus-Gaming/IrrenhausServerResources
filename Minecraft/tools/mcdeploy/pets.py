"""FreeMinecraftModels pets: build models from a recipe and lint the pet files.

A recipe `Minecraft/pets/<species>.yml` describes how the committed models of one species are made:

- `import`: fixes applied to a raw download (`mc pet-build <species> --source file.bbmodel`), which becomes
  the base model `<model>.bbmodel`.
- `recolor`: colour variants `<model>_<colour>.bbmodel`, made from the base model's texture. Only the texture
  pixels of the listed cubes change; each is mapped by brightness onto the variant's gradient.

Lint rebuilds every variant from the committed base model and fails if a file differs, so variants can only
change through their recipe. See Minecraft/docs/pets.md for the whole workflow.
"""
from __future__ import annotations

import base64
import colorsys
import copy
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .errors import McDeployError
from .formats import parse

MODELS_REL = "Minecraft/Backend/plugins/FreeMinecraftModels/models"
PET_PREFIX = "irrenhaus_pet_"
NAME_RE = re.compile(r"^[a-z0-9._-]+$")
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
REQUIRED_ANIMATIONS = ("idle", "walk")
RECIPE_KEYS = {"model", "import", "recolor"}
IMPORT_KEYS = {"strip_animation_prefix", "rename_animations", "copy_animations", "hitbox"}
RECOLOR_KEYS = {"cubes", "variants"}
VARIANT_KEYS = {"gradient", "iris", "pupil"}


def recipes_dir() -> Path:
    return config.MC_DIR / "pets"


def models_dir() -> Path:
    return config.REPO_ROOT / MODELS_REL


def _rgb(value, where: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not COLOUR_RE.match(value):
        raise McDeployError(f"{where}: colour must be '#rrggbb', got {value!r}")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


@dataclass
class Variant:
    name: str
    gradient: list[tuple[float, tuple[int, int, int]]]
    iris: tuple[int, int, int] | None = None
    pupil: tuple[int, int, int] | None = None


@dataclass
class Recipe:
    species: str
    model: str
    strip_prefix: str = ""
    rename: dict[str, str] = field(default_factory=dict)
    copies: dict[str, str] = field(default_factory=dict)
    hitbox: tuple[list[float], list[float]] | None = None
    cubes: list[str] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)

    def variant_model(self, variant: Variant) -> str:
        return f"{self.model}_{variant.name}"


def _unknown(data: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise McDeployError(f"{where}: unknown key(s) {extra}; allowed: {sorted(allowed)}")


def load_recipe(species: str) -> Recipe:
    path = recipes_dir() / f"{species}.yml"
    if not path.is_file():
        raise McDeployError(f"no pet recipe {path.relative_to(config.REPO_ROOT).as_posix()}")
    where = path.relative_to(config.REPO_ROOT).as_posix()
    docs = parse("yaml", path.read_text(encoding="utf-8"))
    data = docs[0] if docs else None
    if not isinstance(data, dict):
        raise McDeployError(f"{where}: expected a mapping")
    _unknown(data, RECIPE_KEYS, where)
    model = data.get("model")
    if not isinstance(model, str) or not model.startswith(PET_PREFIX) or not NAME_RE.match(model):
        raise McDeployError(f"{where}: model must be a lowercase file name starting with {PET_PREFIX}")
    recipe = Recipe(species=species, model=model)
    imp = data.get("import") or {}
    _unknown(imp, IMPORT_KEYS, f"{where} import")
    recipe.strip_prefix = imp.get("strip_animation_prefix") or ""
    recipe.rename = dict(imp.get("rename_animations") or {})
    recipe.copies = dict(imp.get("copy_animations") or {})
    if imp.get("hitbox") is not None:
        hb = imp["hitbox"]
        if not (isinstance(hb, dict) and set(hb) == {"from", "to"}
                and all(isinstance(hb[k], list) and len(hb[k]) == 3 for k in hb)):
            raise McDeployError(f"{where}: import.hitbox needs from: [x, y, z] and to: [x, y, z]")
        recipe.hitbox = ([float(v) for v in hb["from"]], [float(v) for v in hb["to"]])
    rec = data.get("recolor") or {}
    _unknown(rec, RECOLOR_KEYS, f"{where} recolor")
    recipe.cubes = list(rec.get("cubes") or [])
    for name, spec in (rec.get("variants") or {}).items():
        vwhere = f"{where} recolor.variants.{name}"
        if not NAME_RE.match(str(name)):
            raise McDeployError(f"{vwhere}: variant names are lowercase a-z0-9._-")
        if not isinstance(spec, dict):
            raise McDeployError(f"{vwhere}: expected a mapping")
        _unknown(spec, VARIANT_KEYS, vwhere)
        stops = spec.get("gradient")
        if not isinstance(stops, dict) or len(stops) < 2:
            raise McDeployError(f"{vwhere}: gradient needs at least two `brightness: '#rrggbb'` stops (0-255)")
        gradient = sorted((float(k), _rgb(v, vwhere)) for k, v in stops.items())
        if any(not 0 <= k <= 255 for k, _ in gradient):
            raise McDeployError(f"{vwhere}: gradient brightness keys must be 0-255")
        recipe.variants.append(Variant(
            name=str(name), gradient=gradient,
            iris=_rgb(spec["iris"], vwhere) if "iris" in spec else None,
            pupil=_rgb(spec["pupil"], vwhere) if "pupil" in spec else None))
    if recipe.variants and not recipe.cubes:
        raise McDeployError(f"{where}: recolor.cubes lists no cubes")
    return recipe


def recipe_species() -> list[str]:
    folder = recipes_dir()
    return sorted(p.stem for p in folder.glob("*.yml")) if folder.is_dir() else []


# --- bbmodel helpers -------------------------------------------------------------------------------------

def dump_model(model: dict) -> bytes:
    return json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _groups(model: dict) -> dict[str, dict]:
    return {g["uuid"]: g for g in model.get("groups", [])}


def _node_name(node, groups: dict, elements: dict) -> str:
    if isinstance(node, dict):
        return node.get("name") or groups.get(node.get("uuid"), {}).get("name", "?")
    return elements.get(node, {}).get("name", "?")


def structure(model: dict) -> list[str]:
    """Bone and cube names in outliner order, indented by depth (the part every colour must share)."""
    groups = _groups(model)
    elements = {e["uuid"]: e for e in model.get("elements", [])}
    out: list[str] = []

    def walk(nodes, depth):
        for node in nodes:
            kind = "bone" if isinstance(node, dict) else "cube"
            out.append(f"{'  ' * depth}{kind} {_node_name(node, groups, elements)}")
            if isinstance(node, dict):
                walk(node.get("children", []), depth + 1)
    walk(model.get("outliner", []), 0)
    return out


def top_level_hitbox(model: dict) -> list[dict] | None:
    """Cubes of the top-level `hitbox` bone, or None when there is no such bone."""
    groups = _groups(model)
    elements = {e["uuid"]: e for e in model.get("elements", [])}
    for node in model.get("outliner", []):
        if isinstance(node, dict) and _node_name(node, groups, elements) == "hitbox":
            return [elements[c] for c in node.get("children", []) if not isinstance(c, dict) and c in elements]
    return None


def _set_hitbox(model: dict, recipe: Recipe) -> None:
    lo, hi = recipe.hitbox
    groups = _groups(model)
    elements = {e["uuid"]: e for e in model.get("elements", [])}
    old = [n for n in model["outliner"] if isinstance(n, dict) and _node_name(n, groups, elements) == "hitbox"]
    for node in old:
        drop = {node["uuid"], *[c for c in node.get("children", []) if not isinstance(c, dict)]}
        model["outliner"].remove(node)
        model["elements"] = [e for e in model["elements"] if e["uuid"] not in drop]
        model["groups"] = [g for g in model.get("groups", []) if g["uuid"] not in drop]
    cube_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{recipe.model}/hitbox_cube"))
    bone_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{recipe.model}/hitbox_bone"))
    faces = {k: {"uv": [0, 0, 0, 0], "texture": None} for k in ("north", "east", "south", "west", "up", "down")}
    model["elements"].append({
        "name": "hitbox", "box_uv": True, "render_order": "default", "locked": False, "export": True, "scope": 0,
        "allow_mirror_modeling": True, "from": lo, "to": hi, "autouv": 0, "color": 0, "origin": [0, 0, 0],
        "faces": faces, "type": "cube", "uuid": cube_id})
    if "groups" in model:
        model["groups"].append({
            "name": "hitbox", "uuid": bone_id, "export": True, "locked": False, "scope": 0, "selected": False,
            "_static": {"properties": {}, "temp_data": {}}, "origin": [0, 0, 0], "rotation": [0, 0, 0], "color": 0,
            "children": [], "reset": False, "shade": True, "mirror_uv": False, "visibility": False, "autouv": 0,
            "isOpen": False, "primary_selected": False})
        model["outliner"].append({"uuid": bone_id, "isOpen": False, "children": [cube_id]})
    else:  # format 4.x: names inline in the outliner
        model["outliner"].append({"name": "hitbox", "uuid": bone_id, "origin": [0, 0, 0], "rotation": [0, 0, 0],
                                  "export": True, "visibility": False, "children": [cube_id]})


def import_model(raw: dict, recipe: Recipe) -> dict:
    """Apply the recipe's import fixes to a raw download. Idempotent: running it on the base model again
    changes nothing."""
    model = copy.deepcopy(raw)
    model["name"] = recipe.model
    model["model_identifier"] = ""
    model.pop("geckolib_filepath_cache", None)
    anims = model.get("animations", [])
    for anim in anims:
        name = anim["name"]
        if recipe.strip_prefix and name.startswith(recipe.strip_prefix):
            name = name[len(recipe.strip_prefix):]
        anim["name"] = recipe.rename.get(name, name)
        if "path" in anim:
            anim["path"] = ""
    by_name = {a["name"]: a for a in anims}
    for new, src in recipe.copies.items():
        if src not in by_name:
            raise McDeployError(f"pet {recipe.species}: copy_animations source {src!r} not in the model "
                                f"(has {sorted(by_name)})")
        clone = copy.deepcopy(by_name[src])
        clone["name"] = new
        clone["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{recipe.model}/{new}"))
        if new in by_name:
            anims[anims.index(by_name[new])] = clone
        else:
            anims.append(clone)
        by_name[new] = clone
    if recipe.hitbox:
        _set_hitbox(model, recipe)
    textures = model.get("textures", [])
    if len(textures) != 1:
        raise McDeployError(f"pet {recipe.species}: expected exactly one texture, found {len(textures)}")
    textures[0]["name"] = f"{recipe.model}.png"
    for key in ("relative_path", "path"):
        if key in textures[0]:
            textures[0][key] = ""
    return model


# --- recolouring -----------------------------------------------------------------------------------------

def _decode_texture(model: dict):
    from PIL import Image
    src = model["textures"][0].get("source", "")
    if not src.startswith("data:image/png;base64,"):
        raise McDeployError("texture is not an embedded PNG; save the model with the texture embedded")
    return Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))).convert("RGBA")


def _encode_texture(image) -> str:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def fur_mask(model: dict, cubes: list[str], size: tuple[int, int]) -> set[tuple[int, int]]:
    """Texture pixels covered by the faces of the named cubes."""
    scale_x = size[0] / model["resolution"]["width"]
    scale_y = size[1] / model["resolution"]["height"]
    names = {e["name"] for e in model["elements"]}
    missing = sorted(set(cubes) - names)
    if missing:
        raise McDeployError(f"recolor.cubes not in the model: {missing}")
    mask: set[tuple[int, int]] = set()
    for element in model["elements"]:
        if element["name"] not in cubes:
            continue
        for face in element.get("faces", {}).values():
            uv = face.get("uv")
            if not uv or face.get("texture") is None:
                continue
            x0, x1 = sorted((round(uv[0] * scale_x), round(uv[2] * scale_x)))
            y0, y1 = sorted((round(uv[1] * scale_y), round(uv[3] * scale_y)))
            mask.update((x, y) for x in range(x0, x1) for y in range(y0, y1)
                        if 0 <= x < size[0] and 0 <= y < size[1])
    return mask


def gradient_colour(stops, y: float) -> tuple[int, int, int]:
    if y <= stops[0][0]:
        return stops[0][1]
    for (y0, c0), (y1, c1) in zip(stops, stops[1:]):
        if y <= y1:
            t = (y - y0) / (y1 - y0)
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return stops[-1][1]


def recolour_pixel(rgb: tuple[int, int, int], variant: Variant) -> tuple[int, int, int] | None:
    """New colour for one fur pixel, or None to keep it (eye highlight, iris, pupil, pink pads/blush)."""
    r, g, b = rgb
    _, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if rgb == (255, 255, 255):                       # eye highlight
        return None
    if s > 0.55 and v > 0.3:                         # iris (saturated; checked before the paler pinks)
        return tuple(round(c * v) for c in variant.iris) if variant.iris else None
    if r - g > 50 and r > 150:                       # pink pads, nose, blush
        return None
    brightness = 0.299 * r + 0.587 * g + 0.114 * b
    if brightness < 15:                              # pupil
        return variant.pupil
    return gradient_colour(variant.gradient, brightness)


def build_variant(base: dict, recipe: Recipe, variant: Variant) -> tuple[dict, object]:
    image = _decode_texture(base)
    mask = fur_mask(base, recipe.cubes, image.size)
    for x, y in sorted(mask):
        r, g, b, a = image.getpixel((x, y))
        if a == 0:
            continue
        new = recolour_pixel((r, g, b), variant)
        if new is not None:
            image.putpixel((x, y), (*new, a))
    model = copy.deepcopy(base)
    name = recipe.variant_model(variant)
    model["name"] = name
    model["textures"][0]["name"] = f"{name}.png"
    model["textures"][0]["source"] = _encode_texture(image)
    return model, image


def build(recipe: Recipe, base: dict) -> dict[str, bytes]:
    """File name -> bytes for every colour variant of the recipe."""
    return {f"{recipe.variant_model(v)}.bbmodel": dump_model(build_variant(base, recipe, v)[0])
            for v in recipe.variants}


def preview(base: dict, recipe: Recipe, path: Path) -> None:
    """All textures side by side (8x, nearest neighbour) for a look before going in game."""
    from PIL import Image
    images = [_decode_texture(base)] + [build_variant(base, recipe, v)[1] for v in recipe.variants]
    w, h = images[0].size
    scale = max(1, 512 // max(w, h))
    sheet = Image.new("RGBA", (w * scale * len(images), h * scale), (90, 140, 90, 255))
    for i, image in enumerate(images):
        big = image.resize((w * scale, h * scale), Image.NEAREST)
        sheet.paste(big, (i * w * scale, 0), big)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def run_build(species: str, source: str | None) -> None:
    recipe = load_recipe(species)
    folder = models_dir()
    base_path = folder / f"{recipe.model}.bbmodel"
    if source:
        src = Path(source)
        if not src.is_absolute():
            src = config.REPO_ROOT / src
        if not src.is_file():
            raise McDeployError(f"--source {source}: no such file (inside the tools container only the repo is "
                                f"mounted; put downloads under .cache/)")
        base = import_model(json.loads(src.read_text(encoding="utf-8")), recipe)
        base_path.write_bytes(dump_model(base))
        print(f"wrote {base_path.relative_to(config.REPO_ROOT).as_posix()} (imported from {source})")
    elif not base_path.is_file():
        raise McDeployError(f"{base_path.relative_to(config.REPO_ROOT).as_posix()} does not exist; "
                            f"import the raw download with --source")
    base = json.loads(base_path.read_text(encoding="utf-8"))
    for name, data in build(recipe, base).items():
        (folder / name).write_bytes(data)
        print(f"wrote {MODELS_REL}/{name}")
    shot = config.CACHE_DIR / "pets" / f"{species}.png"
    preview(base, recipe, shot)
    print(f"texture preview: {shot.relative_to(config.REPO_ROOT).as_posix()}")
    problems = check()
    if problems:
        print("still to do (mc lint will fail until fixed):")
        for problem in problems:
            print(f"  - {problem}")


# --- lint ------------------------------------------------------------------------------------------------

def _check_model(rel: str, model: dict) -> list[str]:
    problems = []
    cubes = top_level_hitbox(model)
    if cubes is None:
        problems.append(f"{rel}: no top-level `hitbox` bone")
    elif len(cubes) != 1:
        problems.append(f"{rel}: the `hitbox` bone must hold exactly one cube, has {len(cubes)}")
    names = [a["name"] for a in model.get("animations", [])]
    for need in REQUIRED_ANIMATIONS:
        if need not in names:
            problems.append(f"{rel}: no `{need}` animation (has {sorted(names)})")
    bad = sorted(n for n in names if n != n.lower())
    if bad:
        problems.append(f"{rel}: animation names must be lowercase: {bad}")
    return problems


def check() -> list[str]:
    """Lint: every irrenhaus pet file is tracked, pet files point at existing models, models follow the FMM
    authoring rules, colours share bones and animations with the base, and recipe variants are up to date."""
    try:
        return [f"pets: {p}" for p in _check()]
    except McDeployError as exc:
        return [f"pets: {exc}"]


def _check() -> list[str]:
    folder = models_dir()
    if not folder.is_dir():
        return []
    problems: list[str] = []
    tracked = set(config.git_files([MODELS_REL]))
    local = sorted(p.name for p in folder.glob(f"{PET_PREFIX}*") if p.is_file())
    for name in local:
        if f"{MODELS_REL}/{name}" not in tracked:
            problems.append(f"{name} is ignored; add `!{MODELS_REL}/{name}` to .gitignore")
    models: dict[str, dict] = {}

    def load(model_name: str, where: str) -> dict | None:
        if model_name in models:
            return models[model_name]
        path = folder / f"{model_name}.bbmodel"
        if not path.is_file():
            problems.append(f"{where}: model {model_name}.bbmodel does not exist")
            return None
        models[model_name] = json.loads(path.read_text(encoding="utf-8"))
        problems.extend(_check_model(f"{model_name}.bbmodel", models[model_name]))
        return models[model_name]

    for pet in sorted(folder.glob(f"{PET_PREFIX}*.pet.yml")):
        docs = parse("yaml", pet.read_text(encoding="utf-8"))
        data = docs[0] if docs else {}
        if not isinstance(data, dict):
            problems.append(f"{pet.name}: expected a mapping")
            continue
        stem = pet.name[:-len(".pet.yml")]
        if not (folder / f"{stem}.source.md").is_file():
            problems.append(f"{pet.name}: no {stem}.source.md (source, author, license, changes)")
        base_name = data.get("model") or stem
        base = load(base_name, pet.name)
        for entry in data.get("variants") or []:
            v_name = entry.get("model") if isinstance(entry, dict) else str(entry).split(":", 1)[0]
            variant = load(v_name, pet.name) if v_name else None
            if base is not None and variant is not None and v_name != base_name:
                if structure(variant) != structure(base):
                    problems.append(f"{v_name}.bbmodel: bones/cubes differ from {base_name}.bbmodel")
                if sorted(a["name"] for a in variant.get("animations", [])) != \
                        sorted(a["name"] for a in base.get("animations", [])):
                    problems.append(f"{v_name}.bbmodel: animations differ from {base_name}.bbmodel")

    for species in recipe_species():
        recipe = load_recipe(species)
        base_path = folder / f"{recipe.model}.bbmodel"
        if not base_path.is_file():
            problems.append(f"recipe {species}: {recipe.model}.bbmodel does not exist")
            continue
        base = json.loads(base_path.read_text(encoding="utf-8"))
        if dump_model(import_model(base, recipe)) != base_path.read_bytes():
            problems.append(f"{recipe.model}.bbmodel does not match recipe {species}.yml import; "
                            f"run `mc pet-build {species}` with --source")
        for name, data in build(recipe, base).items():
            path = folder / name
            if not path.is_file() or path.read_bytes() != data:
                problems.append(f"{name} is out of date with recipe {species}.yml; run `mc pet-build {species}`")
    return problems
