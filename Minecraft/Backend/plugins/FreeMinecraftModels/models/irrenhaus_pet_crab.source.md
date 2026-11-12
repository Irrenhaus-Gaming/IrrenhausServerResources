# irrenhaus_pet_crab*.bbmodel

- Source: Crab Sailor by @logan_kant, https://blockbenchworkshop.com/model/logan_kant/crab-sailor
- Released under CC0 1.0 (public domain): https://creativecommons.org/publicdomain/zero/1.0/
- Used as the FreeMinecraftModels pet `crab` (`irrenhaus_pet_crab.pet.yml`) with four colour variants:
  `irrenhaus_pet_crab` (original texture), `irrenhaus_pet_crab_blue`, `irrenhaus_pet_crab_green`,
  `irrenhaus_pet_crab_gold`
- Built from the download by `mc pet-build crab` with the recipe `Minecraft/pets/crab.yml`
- Changes (all four files):
  - renamed the animations from `animation.funny_crab.<name>` to `<name>` (FreeMinecraftModels plays `idle`
    and `walk` by name)
  - added `idle.long`, a copy of `drink`, which FreeMinecraftModels plays now and then while the pet stands still,
    and `spawn`, a copy of `salute`, played when the pet is sent out
  - set `dance` and `tipsy` to play once instead of looping; the `idle_tricks` behavior plays them (and `snap`,
    `salute`, `hat_toss`) now and then while the pet stands still, and cannot stop a looping animation
  - added a top-level `hitbox` bone (12 x 10 x 12 px) so FreeMinecraftModels sizes the pet's click box and Bedrock
    hitbox from it instead of falling back to 1 x 2 blocks
  - renamed the model and its texture to the file name, removed the GeckoLib model identifier
- Colour variants: the shell, claws, arms, legs and eye stalks (magenta-to-peach hues only, `recolor.match`) are
  recoloured by brightness onto a blue, green or gold gradient; the dark pupils are unchanged. Cap and bottle are
  unchanged.
