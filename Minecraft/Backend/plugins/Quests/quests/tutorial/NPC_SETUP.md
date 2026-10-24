# Tutorial quest chain: server setup

The quests in `Backend/plugins/Quests/quests/tutorial/` are deployed like every other config file. The NPCs, the first-join trigger and the removal of the old test quest are done by hand, once per server that hosts the tutorial. Citizens stores NPCs in `plugins/Citizens/saves.yml`, which is not tracked, so nothing below can be deployed from the repo.

Run the commands in game as an operator. An NPC spawns exactly where you stand and faces where you look, and `/npc create` selects the new NPC, so the commands after it apply to that NPC.

**The names must match exactly:** capital letters, the `ä` in `Wärter`, one space. The quests find the NPCs by name.

| NPC | Where | Quest steps |
|---|---|---|
| `Wärter Otto` | spawn | 1 (start), 13 (finale) |
| `Wegmeisterin Frieda` | spawn | 3, 4 (takes 16 oak logs) |
| `Gildenbote Kasimir` | spawn, next to the Adventurer's Guild wormhole | 9 |
| `Alter Hase` | next to Wärter Otto | skips the tutorial |

## 1. Remove the old Questgeber

The old test NPC starts the deleted quest `npcquest-tutorial`. Find its id in the list and remove it:

```
/npc list
/npc select <id of Questgeber>
/npc remove
```

## 2. Wärter Otto

Stand at the spawn where Otto should stand, look the way he should face:

```
/npc create Wärter Otto
/npc lookclose
```

Steps 1 and 13 react to a right-click on Otto by themselves; he needs no `/npc command`.

## 3. Wegmeisterin Frieda

```
/npc create Wegmeisterin Frieda
/npc lookclose
```

Step 4 takes the oak logs from the player's inventory when they right-click her.

## 4. Gildenbote Kasimir

Stand next to the wormhole into the Adventurer's Guild:

```
/npc create Gildenbote Kasimir
/npc lookclose
```

## 5. Alter Hase (skip)

Stand next to Wärter Otto:

```
Run this whole block in one go, in this order. `/npc create` selects the new NPC and `/npc command add` adds to the selected NPC, so all 15 commands end up on Alter Hase. Don't select or create another NPC in between.

- `-r` means right-click only.
- Without `-p` the commands run from the console, and Citizens replaces `<p>` with the clicking player's name.
- `--permissions irrenhaus.tutorial.aktiv` limits them to players who started the tutorial and haven't finished it. Step 1 grants the permission; step 13 and the last command here remove it. Without this check, players could reset a finished tutorial and collect the rewards again.

```
/npc create Alter Hase
/npc lookclose
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-01-willkommen
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-02-questbuch
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-03-wegmeisterin
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-04-holz
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-05-bauplatz
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-06-sethome
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-07-spawn
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-08-heimweg
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-09-gildenbote
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-10-wurmloch
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-11-gildenmenue
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-12-gruppe
/npc command add -r --permissions irrenhaus.tutorial.aktiv quests a moddata reset <p> tutorial-13-abschluss
/npc command add -r --permissions irrenhaus.tutorial.aktiv minecraft:tellraw <p> {"text":"Du hast das Tutorial übersprungen. Unter /quests → Tutorial kannst du es jederzeit neu starten.","color":"gray"}
/npc command add -r --permissions irrenhaus.tutorial.aktiv lp user <p> permission unset irrenhaus.tutorial.aktiv
```

Check the result with `/npc command`. It should list these 15 commands in this order. If something went wrong (a typo, or the commands landed on another NPC), select the affected NPC, clear its commands, then select Alter Hase and add all 15 again:

```
/npc list
/npc select <id>
/npc command remove all
```

## 6. Check

```
/npc list
```

The list shows `Wärter Otto`, `Wegmeisterin Frieda`, `Gildenbote Kasimir` and `Alter Hase`.

Play through once with your own account:

```
/quests a moddata start <your name> tutorial-01-willkommen
```

- Right-click Otto: step 2 starts, and the sidebar shows "Ziel: /quests".
- Each finished step starts the next one by itself.
- Right-clicking Alter Hase during the tutorial stops it.
- After the finale, Alter Hase refuses (missing permission).

To start over, right-click Alter Hase while the tutorial is running, or reset every step with the `quests a moddata reset` commands above.

## 7. First-join trigger and old quest file

- In AMP, add a first-join trigger on this server that runs `quests a moddata start <player> tutorial-01-willkommen`, with AMP's variable for the player name.
- Delete `plugins/Quests/quests/npcquests/npcquest-tutorial.yml` on the server. Deploys never delete remote files.
- Restart the instance, or run `/quests a reload`, so the old quest is gone.
