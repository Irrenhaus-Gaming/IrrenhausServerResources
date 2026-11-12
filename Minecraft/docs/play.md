# Starting the local play server (for agents and workflows)

How any workflow that needs to look at something in game (pets, models, holograms, menus) starts and shares the
local play server. Commands are in [README.md](../README.md#visual-checks-local-play-server-and-game-client); this file
is the procedure. Run every command with `MC_NO_GIT=1` (`./mc.sh …` in bash, `mc.cmd …` / `.\mc.ps1 …` on Windows).

## What a play session is

One session per machine: the containers `mcd-play-mysql` (owns the network and the published ports),
`mcd-play-server` and optionally `mcd-play-client`, with fixed names and fixed ports. Every agent, workflow and the
owner share the same one. Two sessions at once are impossible, and stopping it stops it for everyone.

- `play start` publishes the server on `127.0.0.1:25565` and the resource pack on `127.0.0.1:25566`, always and
  only on this PC. Docker Desktop shows both ports on `mcd-play-mysql`, not on the server container.
- The server gets the repo's files **as they are when `play start` renders them**. Later edits to models, pet files
  or configs reach it only through a new start (stop + start).
- Everyone in `Backend/ops.json` is admin (op under the offline-mode UUID), plus the client's player `ClaudeBot`.

## Procedure

1. **Look first, change nothing:** `play status`. It shows which containers exist, the target, the ports, whether the
   server is up, whether the pack is served, and who is online.
2. **Decide:**
   - *No session:* start one (step 3).
   - *Session is up and already has your current files* (you started it in this conversation and changed nothing that
     it renders since): reuse it, go to step 4.
   - *Session exists but you did not start it in this conversation,* or it lacks your changes: it may be the owner's
     or another agent's, and someone may be playing on it. Ask the owner before `play stop`. Never stop it on your
     own, and never on a loop.
   - *Session is "starting" or containers come and go:* another start is running. Wait and check `play status`
     again; don't start a second one.
3. **Start once, in the foreground:** `play start mc-backend-Test`.
   - It takes 1 to 3 minutes (the tool waits up to 15 minutes; give the command a long timeout, or run it in the
     background and wait for it to finish). Never wrap it in a retry loop.
   - Use `--jars-from remote` only when there are no cached jars (`no cached jars` error) or the owner asks for fresh
     ones: it reads the live server over SFTP with the secrets in `.mc-local.env`.
   - If it fails, read the error and `play log`, fix the cause, then start again. A failed start removes only the
     containers it created itself. `a play session exists already` means someone else started one: back to step 1.
   - Success ends with `play server is up` and a `join with your own client …` line.
4. **Wait for the resource pack:** `play status` until `resource pack: served on localhost:25566` (a few seconds
   after "up"; ResourcePackManager builds the pack after startup). Clients that join earlier miss the models.
5. **Tell the owner** that it is up and how to join: Multiplayer → Direct Connection → `localhost:25565`, Minecraft
   version from `play status`, admin rights automatic. Do this every time you start one, before your own checks.
6. **Your client, if you need screenshots:** `client start` (waits until ClaudeBot joined; the first start ever
   downloads the game). If `mcd-play-client` already exists and is not yours, ask as in step 2. Then work with
   `client cmd`/`shot`/`click` and `play console`.
7. **When you are done:** `client stop` for your client. Stop the server (`play stop`) only if you started it and
   `play status` shows no players besides ClaudeBot; otherwise leave it running and tell the owner it is still up.

## After changing files

A running server never picks up repo edits. To test a new version: `play status` (anyone online? then ask), `play
stop`, `play start …`, wait for the pack, tell the owner, `client start` again. Plugin reload commands
(`/fmm reload`) don't help either: the changed files are not in the container.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `a play session exists already` | someone else's session, or one left over: step 1 and 2 |
| containers appear and vanish, ports not reachable | several starts or stops are racing (another agent session or a script in a loop); stop that process, then step 1 |
| `server: not running` | the start failed or the server crashed: `play log`, fix, `play stop` (yours) and start again |
| models missing in the client | joined before the pack was served, or the pack was refused: `client log --grep pack`; rejoin |
| `/tp` answers in German "Spieler … nicht gefunden" | a plugin overrides `/tp`: use `minecraft:tp` |
