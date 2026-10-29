import json
from decimal import Decimal

import pytest

import mcdeploy.config as config
import mcdeploy.deploy as deploy
import mcdeploy.pull as pull
from mcdeploy import shop
from mcdeploy.config import Target
from mcdeploy.errors import McDeployError
from mcdeploy.formats import parse
from mcdeploy.remote import LocalRemote
from mcdeploy.shop import layout as shop_layout
from mcdeploy.shop import pricing, vanilla

LOCK = {
    "version": "26.3",
    "items": {
        **{n: {"rarity": "common", "stack": 64} for n in (
            "cobblestone", "stone", "iron_ingot", "iron_block", "diamond", "stick", "oak_log", "oak_planks",
            "dirt", "bedrock")},
        "diamond_pickaxe": {"rarity": "common", "stack": 1},
        "elytra": {"rarity": "epic", "stack": 1},
        "fancy_wings": {"rarity": "rare", "stack": 1},
    },
    "tags": {"logs": ["oak_log"]},
    "recipes": {
        "iron_block": [{"out": 1, "in": {"iron_ingot": 9}}],
        "iron_ingot": [{"out": 9, "in": {"iron_block": 1}}],
        "stone": [{"out": 1, "in": {"cobblestone": 1}}],
        "oak_planks": [{"out": 4, "in": {"#logs": 1}}],
        "stick": [{"out": 4, "in": {"oak_planks": 2}}],
        "diamond_pickaxe": [{"out": 1, "in": {"diamond": 3, "stick": 2}}],
        "fancy_wings": [{"out": 1, "in": {"elytra": 1, "diamond": 1}}],
    },
}
SHOP_KNOBS = {"shop": {
    "multiplier": 1, "markup": 2, "craft_factor": 1.05,
    "rarity": {"common": 1, "uncommon": 1.15, "rare": 1.3, "epic": 1.5},
    "sell_ratio": {"auto": 0.05, "farm": 0.15, "mined": 0.3, "none": 0},
}}
ITEMS = {
    "exclude": ["bedrock"],
    "held_back": ["elytra"],
    "values": {
        "auto": {"iron_ingot": 30},
        "farm": {"*_log": 4},
        "mined": {"diamond": 500, "elytra": 20000},
        "none": {"cobblestone": 1, "dirt": 1},
    },
}
LAYOUT = {
    "categories": [
        {"name": "resources", "title": "Rohstoffe", "icon": "iron_ingot", "slot": 15,
         "match": ["iron_*", "diamond"]},
        {"name": "building", "title": "Baublöcke", "icon": "stone", "slot": 13, "subs": [
            {"name": "stone", "title": "Stein", "icon": "stone", "match": ["stone", "cobblestone"]},
            {"name": "wood", "title": "Holz", "icon": "oak_log", "match": ["oak_*"]},
        ]},
        {"name": "misc", "title": "Verschiedenes", "icon": "stick", "slot": 33, "rest": True},
    ],
    "retired": ["Z_EverythingElse.yml", "Building/stone.yml"],
}


def prices(items=ITEMS, knobs=SHOP_KNOBS, lock=LOCK):
    k = pricing.load_shop_knobs(knobs)
    return pricing.price_all(lock, pricing.load_items(lock, items), k), k


def test_prices_follow_the_knobs():
    p, _ = prices()
    assert (p["diamond"].buy, p["diamond"].sell, p["diamond"].stack) == (1000, 150, 1)
    # Farm output: token sell price, in a bundle big enough for whole Moneten.
    assert (p["iron_ingot"].stack, p["iron_ingot"].buy, p["iron_ingot"].sell) == (4, 240, 6)
    # "none": sold, never bought back.
    assert (p["cobblestone"].stack, p["cobblestone"].buy, p["cobblestone"].sell) == (4, 8, -1)
    assert "bedrock" not in p


def test_crafted_items_cost_their_ingredients_and_sell_like_them():
    p, _ = prices()
    assert p["iron_block"].value == Decimal("283.50")  # 9 * 30 * 1.05
    assert p["iron_block"].sell_unit == Decimal("13.5")  # 9 * 30 * 0.05, no craft bonus
    assert p["oak_planks"].value == Decimal("1.05")  # via the #logs tag
    assert p["iron_ingot"].value == 30  # a base value is never replaced by a recipe
    pick = p["diamond_pickaxe"]
    assert pick.cls == "crafted" and pick.recipe == {"diamond": 3, "stick": 2}


def test_held_back_items_and_their_products_are_not_offered():
    p, _ = prices()
    assert not p["elytra"].offered
    assert not p["fancy_wings"].offered
    assert p["diamond"].offered


def test_rarity_premium_applies_to_buy_only():
    items = {**ITEMS, "held_back": []}
    p, _ = prices(items)
    wings = p["fancy_wings"]
    assert wings.buy == int((wings.value * 2 * Decimal("1.3")).to_integral_value(rounding="ROUND_CEILING"))
    assert wings.sell_paid <= wings.sell_unit


def test_rounded_prices_keep_the_rules():
    p, k = prices()
    assert pricing.check_prices(p, LOCK, k) == []


def test_recipe_arbitrage_is_reported():
    items = {**ITEMS, "values": {**ITEMS["values"], "mined": {"diamond": 500, "stone": 20}}}
    p, k = prices(items)
    problems = pricing.check_prices(p, LOCK, k)
    assert any("stone: crafting 1 from shop-bought" in pr for pr in problems)


@pytest.mark.parametrize("shop_knobs, message", [
    ({"markup": 1.2}, "markup must be >= 1.5"),
    ({"sell_ratio": {"auto": 0.05, "farm": 0.15, "mined": 0.5, "none": 0}}, "between 0 and 0.35"),
    ({"craft_factor": 0.9}, "craft_factor must be >= 1"),
    ({"rarity": {"common": 1}}, "must list exactly"),
])
def test_knob_limits(shop_knobs, message):
    with pytest.raises(McDeployError, match=message):
        pricing.load_shop_knobs({"shop": {**SHOP_KNOBS["shop"], **shop_knobs}})


@pytest.mark.parametrize("values, message", [
    ({"auto": {"unobtainium": 3}}, "not a 26.3 item"),
    ({"auto": {"diamond": 3}, "mined": {"diamond": 500}}, "listed under both"),
    ({"none": {"bedrock": 1}}, "has a value but is excluded"),
    ({"farm": {"*_nothing": 1}}, "matches no item"),
    ({"farm": {"stone": 0}}, "value must be a number > 0"),
])
def test_items_validation(values, message):
    with pytest.raises(McDeployError, match=message):
        pricing.load_items(LOCK, {"exclude": ["bedrock"], "values": values})


def test_more_specific_glob_wins():
    lock = {**LOCK, "items": {**LOCK["items"], "dead_brain_coral": {"rarity": "common", "stack": 64},
                             "brain_coral": {"rarity": "common", "stack": 64}}}
    source = pricing.load_items(lock, {"values": {"farm": {"*_coral": 4}, "mined": {"dead_*_coral": 2}}})
    assert source.values["dead_brain_coral"] == (2, "mined")
    assert source.values["brain_coral"] == (4, "farm")


def built_files():
    p, _ = prices()
    layout = shop_layout.load_layout(LOCK, LAYOUT)
    shop_layout.place(layout, p)
    return layout, shop_layout.render_files(layout)


def test_layout_places_every_offered_item_once():
    layout, _ = built_files()
    placed = {l.name: [p.key for p in l.items] for l in layout.leaves()}
    assert placed["resources"] == ["iron_block", "iron_ingot", "diamond"]
    assert placed["stone"] == ["stone", "cobblestone"]
    assert placed["wood"] == ["oak_log", "oak_planks"]
    assert placed["misc"] == ["diamond_pickaxe", "dirt", "stick"]  # rest: true


def test_rendered_files_parse_and_follow_the_plugin_schema():
    _, files = built_files()
    esg = "plugins/EconomyShopGUI"
    assert set(files) == {f"{esg}/{kind}/{path}" for kind in ("sections", "shops") for path in (
        "resources.yml", "building.yml", "Building/stone.yml", "Building/wood.yml", "misc.yml",
        "Z_EverythingElse.yml")}
    docs = {rel: parse("yaml", text)[0] for rel, text in files.items()}
    diamond = docs[f"{esg}/shops/resources.yml"]["pages"]["page1"]["items"]["diamond"]
    assert diamond == {"material": "DIAMOND", "buy": 1000, "sell": 150}
    hub = docs[f"{esg}/shops/building.yml"]["pages"]["page1"]
    assert hub["gui-rows"] == 3 and hub["items"]["wood"]["section"] == "wood"
    assert [i["slot"] for i in hub["items"].values()] == [13, 15]
    sub = docs[f"{esg}/sections/Building/stone.yml"]
    assert sub["sub-section"] is True and sub["hidden"] is True and len(sub["item-layout"]) == 6
    assert docs[f"{esg}/sections/Z_EverythingElse.yml"] == {"enable": False}
    # Building/stone.yml is retired but reused by this layout, so it stays a real section.
    assert docs[f"{esg}/sections/Building/stone.yml"]["enable"] is True


def test_pages_hold_28_items():
    lock = {"version": "x", "tags": {}, "recipes": {},
            "items": {f"block_{i:02d}": {"rarity": "common", "stack": 64} for i in range(30)}}
    items = {"values": {"mined": {"block_*": 10}}}
    p = pricing.price_all(lock, pricing.load_items(lock, items), pricing.load_shop_knobs(SHOP_KNOBS))
    layout = shop_layout.load_layout(lock, {"categories": [
        {"name": "all", "title": "Alles", "icon": "block_00", "slot": 0, "rest": True}]})
    shop_layout.place(layout, p)
    pages = parse("yaml", shop_layout.render_files(layout)["plugins/EconomyShopGUI/shops/all.yml"])[0]["pages"]
    assert [len(pg["items"]) for pg in pages.values()] == [28, 2]


@pytest.mark.parametrize("change, message", [
    (lambda l: l["categories"][2].pop("rest"), "exactly one leaf needs rest"),
    (lambda l: l["categories"][2].update(slot=15), "slot 15 is used twice"),
    (lambda l: l["categories"][1]["subs"][1].update(name="stone"), "'stone' is used twice"),
    (lambda l: l["categories"][0].update(icon="nope"), "icon must be a vanilla item"),
])
def test_layout_validation(change, message):
    data = json.loads(json.dumps(LAYOUT))
    change(data)
    with pytest.raises(McDeployError, match=message):
        shop_layout.load_layout(LOCK, data)


def test_lock_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MC_DIR", tmp_path)
    (tmp_path / "shop").mkdir()
    vanilla.lock_path().write_text(vanilla.dump_lock(LOCK), encoding="utf-8")
    assert vanilla.load_lock() == LOCK


def test_lock_from_data_generator_output(tmp_path):
    item_dir = tmp_path / "reports/minecraft/components/item"
    recipe_dir = tmp_path / "data/minecraft/recipe"
    tag_dir = tmp_path / "data/minecraft/tags/item"
    for d in (item_dir, recipe_dir, tag_dir):
        d.mkdir(parents=True)
    (item_dir / "totem_of_undying.json").write_text(json.dumps(
        {"components": {"minecraft:rarity": "uncommon", "minecraft:max_stack_size": 1}}))
    (item_dir / "oak_planks.json").write_text(json.dumps({"components": {}}))
    (tag_dir / "logs.json").write_text(json.dumps({"values": ["#minecraft:oak_logs"]}))
    (tag_dir / "oak_logs.json").write_text(json.dumps({"values": ["minecraft:oak_log", {"id": "minecraft:oak_wood"}]}))
    (recipe_dir / "oak_planks.json").write_text(json.dumps(
        {"type": "minecraft:crafting_shapeless", "ingredients": ["#minecraft:logs"], "result": {"id": "minecraft:oak_planks", "count": 4}}))
    (recipe_dir / "stairs.json").write_text(json.dumps(
        {"type": "minecraft:crafting_shaped", "key": {"#": "minecraft:oak_planks"}, "pattern": ["#  ", "## ", "###"],
         "result": {"id": "minecraft:oak_stairs", "count": 4}}))
    (recipe_dir / "firework.json").write_text(json.dumps({"type": "minecraft:crafting_special_firework_rocket"}))
    lock = vanilla.build_lock(tmp_path, "26.3")
    assert lock["items"] == {"oak_planks": {"rarity": "common", "stack": 64},
                             "totem_of_undying": {"rarity": "uncommon", "stack": 1}}
    assert lock["tags"] == {"logs": ["oak_log", "oak_wood"]}
    assert lock["recipes"] == {"oak_planks": [{"out": 4, "in": {"#logs": 1}}],
                               "oak_stairs": [{"out": 4, "in": {"oak_planks": 6}}]}


# ---------------------------------------------------------------- render, deploy, pull

def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")  # JSON is YAML


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    mc_dir = tmp_path / "repo" / "Minecraft"
    (mc_dir / "Backend/plugins/Plug").mkdir(parents=True)
    (mc_dir / "Backend/plugins/Plug/config.yml").write_text("a: 1\n")
    write_yaml(mc_dir / "shop/items.yml", ITEMS)
    write_yaml(mc_dir / "shop/layout.yml", LAYOUT)
    (mc_dir / "shop" / vanilla.LOCK_NAME).write_text(vanilla.dump_lock(LOCK), encoding="utf-8")
    write_yaml(mc_dir / "knobs.yml", SHOP_KNOBS)
    monkeypatch.setattr(config, "MC_DIR", mc_dir)
    monkeypatch.setattr(config, "KNOBS_FILE", mc_dir / "knobs.yml")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "source_files", lambda source, include_untracked=True: ["plugins/Plug/config.yml"])
    for module in (deploy, pull):
        monkeypatch.setattr(module, "target_placeholders", lambda target: {})
        monkeypatch.setattr(module, "load_mask_scopes", lambda: {})
        monkeypatch.setattr(module, "load_expansions", lambda: {})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ACT", raising=False)
    server = tmp_path / "server"
    server.mkdir()
    return make_target(), LocalRemote(server), server


def make_target(profile="backend"):
    return Target(
        name="mc-backend-Test" if profile == "backend" else "mc-proxy", profile=profile,
        server_name="Test" if profile == "backend" else "", source="Backend", platform="paper",
        java_image="x", server_jar="*.jar", accept_eula=True, memory="1G", boot_timeout_seconds=1, settle_seconds=0,
    )


RESOURCES = "plugins/EconomyShopGUI/shops/resources.yml"
STOCK = "plugins/EconomyShopGUI/sections/Z_EverythingElse.yml"


def test_backends_render_the_generated_shop(repo):
    target, _, _ = repo
    files = config.managed_files(target)
    assert files[0] == "plugins/Plug/config.yml" and RESOURCES in files
    assert config.managed_files(make_target("proxy")) == ["plugins/Plug/config.yml"]
    rendered = deploy.render_target(target, {})
    assert parse("yaml", rendered[RESOURCES].decode())[0]["pages"]["page1"]["items"]["diamond"]["buy"] == 1000


def test_generated_files_must_not_be_in_the_repo(repo, monkeypatch):
    target, _, _ = repo
    monkeypatch.setattr(config, "source_files", lambda source, include_untracked=True: [RESOURCES])
    with pytest.raises(McDeployError, match="must not exist in the repo"):
        config.managed_files(target)


def test_a_knob_change_regenerates_the_files(repo):
    target, _, _ = repo
    diamond = lambda: parse("yaml", config.read_source(target, RESOURCES).decode())[0]["pages"]["page1"]["items"]["diamond"]
    assert diamond()["buy"] == 1000
    knobs = json.loads(json.dumps(SHOP_KNOBS))
    knobs["shop"]["multiplier"] = 2
    write_yaml(config.KNOBS_FILE, knobs)
    assert diamond()["buy"] == 2000


def test_shop_rule_breaks_stop_rendering(repo):
    target, _, _ = repo
    write_yaml(config.MC_DIR / "shop/items.yml", {**ITEMS, "values": {**ITEMS["values"], "mined": {"diamond": 500, "stone": 20}}})
    with pytest.raises(McDeployError, match="arbitrage"):
        config.managed_files(target)


def test_first_deploy_replaces_the_plugins_stock_files(repo):
    target, remote, server = repo
    (server / STOCK).parent.mkdir(parents=True)
    (server / STOCK).write_text("enable: true\ntitle: stock\n")
    deploy.run_deploy(target, dry_run=False, remote=remote, values={})
    assert parse("yaml", (server / STOCK).read_text())[0] == {"enable": False}
    assert (server / RESOURCES).is_file()


def test_server_edit_of_a_deployed_generated_file_is_drift(repo):
    target, remote, server = repo
    deploy.run_deploy(target, dry_run=False, remote=remote, values={})
    (server / RESOURCES).write_text("pages: {}\n")
    knobs = json.loads(json.dumps(SHOP_KNOBS))
    knobs["shop"]["multiplier"] = 2
    write_yaml(config.KNOBS_FILE, knobs)
    with pytest.raises(McDeployError, match="changed on the server"):
        deploy.run_deploy(target, dry_run=False, remote=remote, values={})


def run_pull(target, remote, monkeypatch, add=()):
    monkeypatch.setattr(pull, "deploy_values", lambda: {})
    monkeypatch.setattr(pull, "load_schema", lambda: {})
    monkeypatch.setattr(pull, "load_scan_allowlist", lambda: [])
    monkeypatch.setattr(pull, "SftpRemote", lambda values: remote)
    monkeypatch.setattr(pull, "verify_instance", lambda target, remote, values: None)
    pull.run_pull(target, list(add))


def test_pull_ignores_stock_files_before_the_first_deploy(repo, monkeypatch):
    target, remote, server = repo
    (server / "plugins/Plug").mkdir(parents=True)
    (server / "plugins/Plug/config.yml").write_text("a: 1\n")
    (server / STOCK).parent.mkdir(parents=True)
    (server / STOCK).write_text("enable: true\n")
    run_pull(target, remote, monkeypatch)
    assert not (target.source_dir / STOCK).exists()


def test_pull_refuses_server_edits_of_generated_files(repo, monkeypatch):
    target, remote, server = repo
    deploy.run_deploy(target, dry_run=False, remote=remote, values={})
    (server / RESOURCES).write_text("pages: {}\n")
    with pytest.raises(McDeployError, match="generated from Minecraft/shop/"):
        run_pull(target, remote, monkeypatch)
    assert not (target.source_dir / RESOURCES).exists()
    with pytest.raises(McDeployError, match="--add cannot import"):
        run_pull(target, remote, monkeypatch, add=[RESOURCES])


def test_lint_check_reports_rule_breaks(repo):
    assert shop.check() == []
    write_yaml(config.MC_DIR / "shop/items.yml", {**ITEMS, "values": {**ITEMS["values"], "mined": {"diamond": 500, "stone": 20}}})
    assert any("arbitrage" in p for p in shop.check())
