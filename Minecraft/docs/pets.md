# Adding a FreeMinecraftModels pet

How a new pet species (and its colour variants) gets from a Blockbench model to the server.
Worked examples: `wolfu` (one colour) and `raccoon` (four colours), both in
`Backend/plugins/FreeMinecraftModels/models/`.

Reference: the FreeMinecraftModels wiki, [pets](https://wiki.nightbreak.io/FreeMinecraftModels/pets) and
[model authoring notes](https://wiki.nightbreak.io/FreeMinecraftModels/model_authoring_notes).

## 1. Pick a model and check its license

- The repository is public and the model ships to every player, so the license must allow redistribution
  and changes. CC0 and CC BY are fine (CC BY: credit the author in `.source.md`). No license, "personal use only"
  or "no redistribution" means no.
- Note the author, the page URL and the license link; they go into `.source.md`.
- Blockbench Workshop downloads need a signed-in account, so the owner downloads the `.bbmodel` by hand. Keep the raw
  download outside the tracked tree, e.g. `.cache/pet-sources/<name>.bbmodel` (`.cache/` is git-ignored).
- Look at what the model contains before planning: bones, cube names, animations, texture size
  (a short Python script over the JSON is enough; a 5.x file joins `outliner` UUIDs to `groups`/`elements`).

## 2. Recipe and import

Each species built by the tooling has a recipe `Minecraft/pets/<species>.yml` (example: `raccoon.yml`). It records
every change made to the download, so the models can be rebuilt at any time and lint can prove they are current.

```yaml
model: irrenhaus_pet_<species>         # base model file name; the model id, never rename it later
import:                                # applied to the raw download with --source
  strip_animation_prefix: animation.<name>.
  rename_animations: {walking: walk}   # optional
  copy_animations: {idle.long: sneeze} # optional: new name <- existing animation
  play_once: [dance]                   # optional: looping animations idle_tricks plays (see section 4)
  hitbox: {from: [-4, 0, -7], to: [4, 16, 7]}
recolor:
  cubes: [body, head, ...]             # cubes whose texture is fur
  variants:
    <colour>:
      gradient: {0: '#1e100c', 95: '#964e24', 240: '#f8ecda'}   # original brightness 0-255 -> new colour
      iris: '#ff3c50'                  # optional, otherwise eyes keep their colour
      pupil: '#6e1420'                 # optional
```

Write the `import` part first, then build the base model:

```bash
MC_NO_GIT=1 ./mc.sh pet-build <species> --source .cache/pet-sources/<download>.bbmodel
```

It writes `irrenhaus_pet_<species>.bbmodel` (and the colours, if any). It also lists what lint still misses
(`.gitignore` lines, `.source.md`, pet file). The download must be under the repo, because the tools container only
sees the repo.

What the model needs (the import covers the first three; the model authoring notes have the details):

- **`hitbox` bone**: top-level, exactly one cube, sized around body and head (width x, height y, depth z, in px).
  It is never rendered or animated. Without it FMM falls back to a 1 x 2 block box.
- **Animation names**: lowercase. FMM plays them by name: `idle` (loops), `walk` (loops), `idle.long` (plays now and
  then while standing; dotted names fall back to `idle`), `spawn`, `attack`, `death`, `swim`/`idle.swim`,
  `ride`/`idle.ride`. Other animations may stay; they cost nothing. Lint requires `idle` and `walk`.
- **Names**: the import sets the model and texture `name` to the file name and clears export paths that point at the
  author's disk.
- **Bones**: no cubes outside groups; `b_`/`tag_` bones stay cube-free; `h_` marks head bones.
- **Texture**: one embedded PNG. Bedrock and GeckoLib `.bbmodel` files both load, and a texture larger than the UV
  size (e.g. 128 px on 64 px UVs) works.
- **Facing**: the face points to Blockbench north (-Z).
- Known FMM limit (2.13.0): walking pets never turn toward where they walk. Don't work around it; see `CLAUDE.md`.

A model edited by hand in Blockbench needs no recipe: then lint only checks the rules above.

## 3. Colour variants

FMM has no texture swap: every colour is its own `.bbmodel` (`irrenhaus_pet_<species>_<colour>.bbmodel`) with the
same bones and animations, listed under `variants:` in the pet file. `pet-build` makes them from the base model:

1. `recolor.cubes` lists the fur cubes. Their faces' UV rectangles mark the pixels that may change. Gear (hat,
   scarf, backpack) is left out, so it keeps its colour. For the cube names, see the model's outliner in Blockbench.
2. Each fur pixel is mapped by its brightness onto the variant's `gradient`, so the original shading survives. To
   pick the stops, look at the brightness of the texture's main shades (dark mask, fur, light belly).
3. Pixels that are not fur colour stay: the white eye highlight, saturated irises, near-black pupils, pink pads and
   blush. `iris`/`pupil` recolour the eyes (the albino gets red ones).
   - If the main colour is itself saturated (the crab's red shell), step 3 would keep almost every pixel. Set
     `recolor.match: {hue: [from, to], min_saturation: s}` instead (degrees, `from > to` wraps past red; example
     `crab.yml`): exactly the pixels in that hue window change, everything else (pupils, gear) keeps its colour.
     `iris`/`pupil` don't apply then.
4. Look at `.cache/pets/<species>.png` (all textures side by side) before going in game. Adjust the gradient and run
   `pet-build <species>` again; without `--source` it rebuilds only the colours.

Never edit a colour `.bbmodel` by hand: lint rebuilds every colour from the recipe and fails on any difference.
A new or changed base model (re-import) changes every colour, so rebuild after it.

Give the base colour the highest `weight` in the pet file (rarity is measured against the most common colour:
common ≥ half, uncommon ≥ a fifth, rare below).

## 4. Pet file and source notes

`irrenhaus_pet_<species>.pet.yml` (see the raccoon file for a full example with variants):

- `species` is stored in every owned pet: never rename it.
- `body`: the hidden vanilla entity (`WOLF`, `FOX`, …). The visual checks find the pet with `@e[type=<body>]`.
- `tamingFood`, `breedingFood`, `icon` (the item shown in `/fmm pets`), `locomotion`, `follow`, `behaviors`
  (Lua modules from `pet_behaviors/`).
- `name` and variant names are German, with `&` colour codes.
- More idle animations: FMM itself plays only one, `idle.long`. Our module `idle_tricks`
  (`pet_behaviors/irrenhaus/idle_tricks.lua`, tracked; FMM never overwrites files with names of its own) plays a
  random one from a list now and then while the pet stands still, and holds it still meanwhile (example: crab):

  ```yaml
  behaviors:
    - stay
    - follow_owner
    - wander
    - idle_look
    - idle_tricks:
        animations: ['dance:2', 'snap:1']   # 'animation:seconds it holds still' (the animation's length)
        rest_seconds: 15                    # a trick every 15 + 0..30 seconds
        rest_random_seconds: 30
  ```

  The pet Lua API can start an animation but not stop it, so every listed animation must play once: put looping
  ones in the recipe's `import.play_once`. `c.pet.speed` is not the pet's current speed (it never drops near 0
  standing still); the module measures movement from `c.pet.position`. A `behaviors` list replaces the default one,
  so it lists `stay`, `follow_owner`, `wander` and `idle_look` too. New modules go in `pet_behaviors/irrenhaus/`
  (the rest of `pet_behaviors/` is FMM's and git-ignored); the module id must be `fmm:pet/<file name>`, and every
  behavior needs a `tick` function or FMM skips the module.
- `wildSpawn` only matters in CREATURES mode; the server runs COSMETIC (`pets.yml`).

`irrenhaus_pet_<species>.source.md`: source URL, author, license, which pet uses it, and every change made to the
model (hitbox, renames, recolours). It is repo-only.

Re-include every new file in the root `.gitignore` (the models folder is ignored), next to the existing
`irrenhaus_pet_*` lines: the `.bbmodel` of each colour, the `.pet.yml` and the `.source.md`.

## 5. Check

```bash
MC_NO_GIT=1 ./mc.sh lint
MC_NO_GIT=1 ./mc.sh gitleaks
```

Lint checks every `irrenhaus_pet_*` file in the models folder:
- it is tracked (not ignored)
- each pet file has its `.source.md` and points at existing models
- each model has a one-cube `hitbox` and `idle`/`walk`
- each colour shares the base's bones and animations
- recipe-built models match their recipe

## 6. Test on the local play server

Start and share the server as described in [play.md](play.md): `play status` first, never stop a session you didn't
start without asking the owner, start once in the foreground, wait for the pack, tell the owner how to join. The
server only has the pet files from the moment it was started, so after `pet-build` or a pet file edit it needs a new
start (stop + start, under the same rules).

```bash
MC_NO_GIT=1 ./mc.sh play status                    # existing session? who is online? (play.md, steps 1-2)
MC_NO_GIT=1 ./mc.sh play start mc-backend-Test     # only if there is none, or after the owner agreed to a restart
MC_NO_GIT=1 ./mc.sh play status                    # until "resource pack: served on localhost:25566"
MC_NO_GIT=1 ./mc.sh play log --grep "pet species"  # "Loaded N pet species" counts the new one
MC_NO_GIT=1 ./mc.sh client start
MC_NO_GIT=1 ./mc.sh client cmd "/fmm petadmin give <species>"
MC_NO_GIT=1 ./mc.sh client shot --name picker      # colour picker: one slot per variant, then "random"
```

- In the picker the variants sit in `variants:` order from `--at 496 280`, one slot every 36 px
  (532, 568, …). Clicking one gives the pet and sends it out. Wait a few seconds before freezing: it walks in from
  where it appeared.
- `/fmm pets` lists owned pets; clicking one sends it home or brings it back.
- Pets wander and follow, so freeze the game before taking pictures, and set the camera with explicit yaw/pitch
  (`facing entity` did not pitch the camera down reliably):

  ```bash
  MC_NO_GIT=1 ./mc.sh play console "tick freeze"
  MC_NO_GIT=1 ./mc.sh play console "execute as @e[type=fox,sort=nearest,limit=1] at @s positioned ~-2.6 ~0.2 ~ run minecraft:tp ClaudeBot ~ ~ ~ -90 30"
  MC_NO_GIT=1 ./mc.sh client shot --name front --no-hud
  MC_NO_GIT=1 ./mc.sh play console "tick unfreeze"
  ```

  Yaw: -90 looks east (+X), 0 south (+Z), 90 west, 180 north. Pets don't face a fixed way, so take a few angles.
- Check every colour: texture, face, animations playing, nothing floating or missing.
- Stop the game client (`client stop`) but leave the server running for step 7.

## 7. Owner test before deploying

The last step before deploying is always a fresh play server that the owner joins and tries the pet in:

```bash
MC_NO_GIT=1 ./mc.sh play status                  # your session from step 6? nobody else online? (else ask, play.md)
MC_NO_GIT=1 ./mc.sh play stop                    # restart so it loads the final files
MC_NO_GIT=1 ./mc.sh play start mc-backend-Test
MC_NO_GIT=1 ./mc.sh play status                  # until "resource pack: served on localhost:25566"
```

When the server is up and the pack is served, tell the owner how to join:

- Multiplayer -> Direct Connection -> `localhost:25565`, with the server's Minecraft version (`play start` prints it).
- Everyone in `Backend/ops.json` is admin. Anyone else: `mc play console "op <name>"`.
- Try it: `/fmm petadmin give <species>`, pick each colour, `/fmm pets` to send pets out and home, walk around
  with them.

The server listens on `127.0.0.1` only (this PC). It runs in offline mode, so anyone who could reach it could join
under any name; never publish it on other interfaces. Leave it running until the owner says they are done, then
`play stop`. Changes go back to step 2 or 3; deploy only after the owner approves.

## 8. Deploy

The owner commits the files, then deploys the usual way: `mc drift` / `mc deploy` or the workflow. FMM builds the
resource pack from the models on the server, as it does for `wolfu`. Deploys never restart servers, so the pet appears
after the next restart or FMM reload.
