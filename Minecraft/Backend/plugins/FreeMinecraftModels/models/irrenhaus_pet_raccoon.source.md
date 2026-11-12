# irrenhaus_pet_raccoon*.bbmodel

- Source: Raccoon Trader by @logan_kant, https://blockbenchworkshop.com/model/logan_kant/raccoon-trader
- Released under CC0 1.0 (public domain): https://creativecommons.org/publicdomain/zero/1.0/
- Used as the FreeMinecraftModels pet `raccoon` (`irrenhaus_pet_raccoon.pet.yml`) with four colour variants:
  `irrenhaus_pet_raccoon` (original texture), `irrenhaus_pet_raccoon_ginger`, `irrenhaus_pet_raccoon_black`,
  `irrenhaus_pet_raccoon_albino`
- Built from the download by `mc pet-build raccoon` with the recipe `Minecraft/pets/raccoon.yml`
- Changes (all four files):
  - renamed the animations from `animation.raccoon_trader.<name>` to `<name>` (FreeMinecraftModels plays `idle`
    and `walk` by name)
  - added `idle.long`, a copy of `sneeze`, which FreeMinecraftModels plays now and then while the pet stands still
  - added a top-level `hitbox` bone (8 x 16 x 14 px) so FreeMinecraftModels sizes the pet's click box and Bedrock
    hitbox from it instead of falling back to 1 x 2 blocks
  - renamed the model and its texture to the file name, removed the GeckoLib export paths
- Colour variants: the fur (body, head, ears, arms, legs, feet, tail) is recoloured by brightness onto a ginger,
  black or albino gradient; eye highlights, irises, pupils and pink pads/blush keep their colour (the albino gets
  red eyes). Hat, scarf, feather, flower and backpack are unchanged.
