# irrenhaus_flight_dragon.bbmodel

- Source: dragon by Loy, https://github.com/SL0ANE/Loy-s-Goodies/blob/main/models/generic-model/creatures/211029_dragon.bbmodel
- Released under CC0: https://github.com/SL0ANE/Loy-s-Goodies/blob/main/LICENSE
- Changes for the EliteMobs flight mount (`EliteMobs/custombosses/irrenhaus_flight_mount.yml`):
  - scaled to 0.4 and moved so the middle of its back sits on the base entity's seat
  - face UVs converted from 16x16 space to texture pixels, as FreeMinecraftModels expects
  - back leg bones renamed from `b_leg` / `b_leg2` to `back_leg` / `back_leg2`: FreeMinecraftModels reserves the `b_`
    prefix for cube-free, non-display bones
  - the `flying` loop re-posed as a level glide (body level, no bob, legs trailing, smaller wing upstroke) and used as both `idle` and `walk`; `static` removed
