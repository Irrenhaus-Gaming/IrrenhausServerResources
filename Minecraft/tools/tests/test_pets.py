import base64
import io
import json

import pytest
from PIL import Image

import mcdeploy.config as config
from mcdeploy import pets
from mcdeploy.errors import McDeployError

FUR = (138, 108, 89)        # grey-brown fur
MASK = (48, 35, 42)         # dark face mask
HIGHLIGHT = (255, 255, 255)
PINK = (235, 167, 155)
IRIS = (176, 106, 46)
PUPIL = (12, 7, 8)
HAT = (110, 26, 32)


def _png(pixels: dict[tuple[int, int], tuple[int, int, int]]) -> str:
    image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    for xy, rgb in pixels.items():
        image.putpixel(xy, (*rgb, 255))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _faces(uv):
    return {k: {"uv": uv, "texture": 0} for k in ("north", "east", "south", "west", "up", "down")}


def raw_model() -> dict:
    """A tiny 5.x model: fur cube on texture x 0-3, hat cube on x 4-7 (4 px UV = 8 px texture)."""
    pixels = {(0, 0): FUR, (1, 0): MASK, (2, 0): HIGHLIGHT, (3, 0): PINK, (0, 1): IRIS, (1, 1): PUPIL,
              (4, 0): HAT, (5, 0): FUR}
    return {
        "meta": {"format_version": "5.0", "model_format": "geckolib_model", "box_uv": True},
        "name": "raw.geo", "model_identifier": "Raw", "geckolib_filepath_cache": {"x": 1},
        "resolution": {"width": 4, "height": 4},
        "elements": [
            {"name": "body", "uuid": "e1", "from": [0, 0, 0], "to": [1, 1, 1], "faces": _faces([0, 0, 2, 4])},
            {"name": "hat", "uuid": "e2", "from": [0, 1, 0], "to": [1, 2, 1], "faces": _faces([2, 0, 4, 4])},
        ],
        "groups": [{"name": "root", "uuid": "g1"}, {"name": "hat", "uuid": "g2"}],
        "outliner": [{"uuid": "g1", "children": ["e1", {"uuid": "g2", "children": ["e2"]}]}],
        "textures": [{"name": "raw.png", "relative_path": "../x/raw.png", "source": _png(pixels)}],
        "animations": [{"name": f"animation.raw.{n}", "uuid": f"a-{n}", "path": "raw.animation.json"}
                       for n in ("idle", "walk", "sneeze")],
    }


RECIPE_YAML = """\
model: irrenhaus_pet_test
import:
  strip_animation_prefix: animation.raw.
  copy_animations: {idle.long: sneeze}
  hitbox: {from: [-4, 0, -7], to: [4, 16, 7]}
recolor:
  cubes: [body]
  variants:
    ginger:
      gradient: {0: '#000000', 255: '#ff8000'}
    albino:
      gradient: {0: '#ffffff', 255: '#ffffff'}
      iris: '#ff0000'
      pupil: '#800000'
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    mc_dir = tmp_path / "Minecraft"
    (mc_dir / "pets").mkdir(parents=True)
    (mc_dir / "pets" / "test.yml").write_text(RECIPE_YAML, encoding="utf-8")
    models = tmp_path / pets.MODELS_REL
    models.mkdir(parents=True)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / ".cache")
    monkeypatch.setattr(config, "git_files", lambda paths, include_untracked=True: sorted(
        f"{pets.MODELS_REL}/{p.name}" for p in models.iterdir()))
    return tmp_path


def _texture(model):
    return pets._decode_texture(model)


def test_import_fixes_names_hitbox_and_is_idempotent(repo):
    recipe = pets.load_recipe("test")
    base = pets.import_model(raw_model(), recipe)
    assert [a["name"] for a in base["animations"]] == ["idle", "walk", "sneeze", "idle.long"]
    assert all(a["path"] == "" for a in base["animations"])
    assert base["name"] == "irrenhaus_pet_test" and base["model_identifier"] == ""
    assert "geckolib_filepath_cache" not in base
    assert base["textures"][0]["name"] == "irrenhaus_pet_test.png" and base["textures"][0]["relative_path"] == ""
    cubes = pets.top_level_hitbox(base)
    assert len(cubes) == 1 and cubes[0]["to"] == [4.0, 16.0, 7.0]
    assert pets.dump_model(pets.import_model(base, recipe)) == pets.dump_model(base)


def test_recolour_changes_only_fur_of_listed_cubes(repo):
    recipe = pets.load_recipe("test")
    base = pets.import_model(raw_model(), recipe)
    ginger = next(v for v in recipe.variants if v.name == "ginger")
    model, image = pets.build_variant(base, recipe, ginger)
    px = lambda x, y: image.getpixel((x, y))[:3]
    assert px(0, 0) != FUR and px(0, 0)[2] == 0          # fur moved onto the black->orange gradient
    assert px(1, 0) != MASK
    assert px(2, 0) == HIGHLIGHT and px(3, 0) == PINK      # kept
    assert px(0, 1) == IRIS and px(1, 1) == PUPIL          # no iris/pupil set: kept
    assert px(4, 0) == HAT and px(5, 0) == FUR             # hat cube is not in recolor.cubes
    assert model["name"] == "irrenhaus_pet_test_ginger"
    assert model["textures"][0]["name"] == "irrenhaus_pet_test_ginger.png"
    assert pets.structure(model) == pets.structure(base)


def test_recolour_sets_iris_and_pupil_when_given(repo):
    recipe = pets.load_recipe("test")
    base = pets.import_model(raw_model(), recipe)
    albino = next(v for v in recipe.variants if v.name == "albino")
    _, image = pets.build_variant(base, recipe, albino)
    assert image.getpixel((1, 1))[:3] == (128, 0, 0)
    r, g, b = image.getpixel((0, 1))[:3]
    assert r > 0 and g == 0 and b == 0


def _write_pet(repo, extra_variants=True):
    folder = repo / pets.MODELS_REL
    recipe = pets.load_recipe("test")
    base = pets.import_model(raw_model(), recipe)
    (folder / "irrenhaus_pet_test.bbmodel").write_bytes(pets.dump_model(base))
    for name, data in pets.build(recipe, base).items():
        (folder / name).write_bytes(data)
    variants = "\n".join(f"  - model: irrenhaus_pet_test_{v}" for v in ("ginger", "albino")) if extra_variants else ""
    (folder / "irrenhaus_pet_test.pet.yml").write_text(
        f"species: test\nmodel: irrenhaus_pet_test\nvariants:\n  - model: irrenhaus_pet_test\n{variants}\n",
        encoding="utf-8")
    (folder / "irrenhaus_pet_test.source.md").write_text("# source\n", encoding="utf-8")
    return folder


def test_check_passes_for_a_complete_pet(repo):
    _write_pet(repo)
    assert pets.check() == []


def test_check_flags_stale_variant_and_missing_parts(repo, monkeypatch):
    folder = _write_pet(repo)
    model = json.loads((folder / "irrenhaus_pet_test_ginger.bbmodel").read_text(encoding="utf-8"))
    model["animations"] = [a for a in model["animations"] if a["name"] != "walk"]
    (folder / "irrenhaus_pet_test_ginger.bbmodel").write_bytes(pets.dump_model(model))
    (folder / "irrenhaus_pet_test.source.md").unlink()
    monkeypatch.setattr(config, "git_files", lambda paths, include_untracked=True: [])
    problems = "\n".join(pets.check())
    assert "irrenhaus_pet_test_ginger.bbmodel: no `walk` animation" in problems
    assert "animations differ from irrenhaus_pet_test.bbmodel" in problems
    assert "out of date with recipe test.yml" in problems
    assert "no irrenhaus_pet_test.source.md" in problems
    assert "irrenhaus_pet_test.pet.yml is ignored" in problems


def test_check_requires_hitbox(repo):
    folder = _write_pet(repo, extra_variants=False)
    (repo / "Minecraft" / "pets" / "test.yml").unlink()
    model = json.loads((folder / "irrenhaus_pet_test.bbmodel").read_text(encoding="utf-8"))
    model["outliner"] = [n for n in model["outliner"] if n.get("uuid") == "g1"]
    (folder / "irrenhaus_pet_test.bbmodel").write_bytes(pets.dump_model(model))
    assert any("no top-level `hitbox` bone" in p for p in pets.check())


def test_run_build_writes_variants_and_preview(repo):
    src = repo / ".cache" / "pet-sources" / "raw.bbmodel"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps(raw_model()), encoding="utf-8")
    pets.run_build("test", ".cache/pet-sources/raw.bbmodel")
    folder = repo / pets.MODELS_REL
    assert {p.name for p in folder.iterdir()} == {
        "irrenhaus_pet_test.bbmodel", "irrenhaus_pet_test_ginger.bbmodel", "irrenhaus_pet_test_albino.bbmodel"}
    assert (repo / ".cache" / "pets" / "test.png").is_file()


def test_recipe_rejects_unknown_keys_and_bad_colours(repo):
    path = repo / "Minecraft" / "pets" / "test.yml"
    path.write_text(RECIPE_YAML.replace("strip_animation_prefix", "strip_prefix"), encoding="utf-8")
    with pytest.raises(McDeployError, match="unknown key"):
        pets.load_recipe("test")
    path.write_text(RECIPE_YAML.replace("'#ff8000'", "orange"), encoding="utf-8")
    with pytest.raises(McDeployError, match="#rrggbb"):
        pets.load_recipe("test")
