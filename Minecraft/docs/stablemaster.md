# Stallmeister: pet selection NPC

Players don't open the FreeMinecraftModels pet menu with `/fmm pets`. They right-click the Citizens NPC
`Stallmeister Anton` at spawn instead, which opens the same menu for them. Clicking a pet there brings it out
or sends it home; shift-right-click twice sets it free. `pets.yml` allows one pet out at a time
(`maxEquippedPets: 1`), so players send the current pet home before picking another one.

Players catch pets in the wild: the server runs FMM in `CREATURES` mode, so every species spawns in the biomes of
its `wildSpawn` and is tamed by feeding it its `tamingFood` (`models/irrenhaus_pet_*.pet.yml`). Tutorial step 13
(`tutorial-13-stallmeister`, quests in `Backend/plugins/Quests/quests/tutorial/`) sends new players to the
Stallmeister, who explains this; keep its species list in line with the pet files. On a server with the tutorial,
place him next to Wärter Otto (`NPC_SETUP.md` in that folder).

The pet messages in `pets.yml` point players to the Stallmeister. The NPC and the permission change are done by
hand, once per server (Citizens keeps NPCs in the untracked `plugins/Citizens/saves.yml`, LuckPerms keeps
permissions in its database), so nothing below is deployed from the repo.

## How it works

FMM's pet menu only opens through `/fmm pets`, a player-only command guarded by `freeminecraftmodels.pets`
(default: everyone). Clicks inside the menu are not checked against any permission. So:

1. Players lose `freeminecraftmodels.pets` (LuckPerms, `default` group): `/fmm pets` says they lack permission.
2. A right-click on the Stallmeister gives the clicking player that permission for 5 seconds (console command),
   then, 10 ticks later so LuckPerms has applied it, runs `fmm pets` as that player.

The player could type `/fmm pets` in those 5 seconds; that is the same menu, so it doesn't matter.

## 1. Take /fmm pets away from players

Run once in game or from the console. LuckPerms data is shared through the database, so this applies on every
server that uses it:

```
/lp group default permission set freeminecraftmodels.pets false
```

Staff groups that should keep `/fmm pets` get it back explicitly, e.g.
`/lp group admin permission set freeminecraftmodels.pets true`. Check a player with
`/lp user <name> permission check freeminecraftmodels.pets`: it must say `false` (from group `default`).

## 2. Create the NPC

Stand where the Stallmeister should stand, look the way he should face, then run this block in one go. `/npc create`
selects the new NPC and every following command applies to it; don't select or create another NPC in between.

- `-r`: right-click only. Without `-p` a command runs from the console, and Citizens replaces `<p>` with the
  clicking player's name; `-p` runs it as the player.
- `--delay 10` waits 10 ticks so the LuckPerms change from the first command is applied.
- `--cooldown 3`: one use per player every 3 seconds, so spam-clicking doesn't flood the console.

```
/npc create Stallmeister Anton
/npc lookclose
/npc command add -r --cooldown 3 lp user <p> permission settemp freeminecraftmodels.pets true 5s replace
/npc command add -r -p --delay 10 --cooldown 3 fmm pets
```

Optional: `/npc skin <player name>` for a fitting look.

To redo the commands: `/npc select <id>` (from `/npc list`), `/npc command remove all`, then the two
`command add` lines again.

## 3. Check

With a normal player account (not op, not staff):

- `/fmm pets` is refused.
- Without pets, a right-click on the Stallmeister makes him say "Du hast ja noch gar keine Haustiere! Zieh los und
  fang dir welche." (`noPetsMessage` in `pets.yml`; it names the NPC, so keep it in line with the NPC's name).
- After `/fmm givepet <name> <species>` (console), a right-click opens "Your pets". Clicking the pet sends it home,
  clicking again brings it back. With two pets, bringing out the second one while the first is out shows
  "You can only have one pet with you".
- Five seconds after the click, `/fmm pets` is refused again.

## Locally (`mc play`)

The play server runs on a throwaway database and its own copy of the server files, so neither the permission nor
the NPC exists there: run steps 1 and 2 on it to try them. Everyone in `Backend/ops.json` is op there, which does
not override the `false` from step 1, so test the refusal with a second, non-op account or check it with
`/lp user <name> permission check freeminecraftmodels.pets`.
