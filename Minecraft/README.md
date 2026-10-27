# Minecraft config

Config for the Irrenhaus Minecraft network: a Velocity proxy (`Proxy/`) and Paper backends (`Backend/`), hosted on AMP.
The repo is public, so secrets are stored as placeholders and filled in from GitHub secrets at deploy time.

## Servers = GitHub Environments

Every server is one GitHub Environment (Settings → Environments). The workflow discovers them by name:

| Environment | What it is |
|---|---|
| `mc-proxy` | the Velocity proxy, deployed with `Proxy/` |
| `mc-backend-<Name>` | a Paper backend, deployed with `Backend/` and listed in `velocity.toml` as `<Name>` |
| `mc-server-<Name>` | a server only listed in `velocity.toml`; its config is not managed here |

`<Name>` is the Velocity server name, case included. It may contain letters, digits, `_` and `-`. **Adding a server = creating its environment with the secrets below**, then running the workflow (Actions → Minecraft config → Run workflow). Nothing in the repo changes.

SFTP lands in the instance's main folder (the folder with the server jar; `plugins/` is a subfolder), so every repo path maps 1:1 to that folder. No remote root needs configuring.

An environment's protection rules are its approval: add required reviewers to `mc-proxy` and production backends, and leave test backends unprotected so they deploy automatically.

### Secrets per environment

| Secret | proxy | backend | listed | Meaning |
|---|:-:|:-:|:-:|---|
| `MC_SERVER_HOST` | ✓ | ✓ | ✓ | the machine's host/IP. Used for SFTP, and by Velocity unless `MC_SERVER_PROXY_HOST` is set |
| `MC_SERVER_PROXY_HOST` |  | optional | optional | address Velocity should use instead, e.g. `127.0.0.1` for a server on the proxy's machine |
| `MC_SERVER_MC_PORT` |  | ✓ | ✓ | Minecraft port, for `velocity.toml` |
| `MC_SERVER_SFTP_PORT` | ✓ | ✓ |  | the AMP instance's SFTP port |
| `AMP_SFTP_HOST_KEY` | ✓ | ✓ |  | pinned host key, e.g. `ssh-ed25519 AAAA...` (`mc sftp-probe --host-key-only`) |
| `MC_DB_USERNAME`, `MC_DB_PASSWORD` | ✓ | ✓ |  | this server's **own** MySQL user. Never set these at repository level |
| `MC_VELOCITY_FORWARDING_SECRET` |  | ✓ |  | must equal the proxy's server-only `forwarding.secret` file |

### Repository secrets (shared)

| Secret | Meaning |
|---|---|
| `MC_DB_HOST`, `MC_DB_PORT` | the one MySQL server all servers use |
| `MC_DISCORD_BOT_TOKEN` | the single Discord bot |
| `AMP_SFTP_USER`, `AMP_SFTP_PASSWORD` | the one AMP user dedicated to GitHub deploys (file access on every deployed instance) |
| `MC_COLLECT_KEY` | key for the encrypted hand-over below; create it with `mc gen-collect-key` |

### How velocity.toml gets its server list

A GitHub job only sees the secrets of its own environment, so the proxy job cannot read the backend environments. Every proxy deploy therefore first runs one small job in each `mc-backend-*` / `mc-server-*` environment. A backend that is deployed in the same run does this in its own job. That job encrypts its entry (`<Name> = "host:port"`) with `MC_COLLECT_KEY` and hands it over as a workflow artifact; only ciphertext is published. The proxy job decrypts the entries and checks that exactly the expected environments delivered one in this run, then expands them into `[servers]` (placeholder `MC_VELOCITY_SERVERS`; backends first, then listed servers, each sorted by environment name).

Consequences:
- a proxy deploy also waits for approval in every **protected** server environment
- `try = ["test", "__SECRET_MC_VELOCITY_SERVERS__"]`: servers named by hand come first, the placeholder appends every other server in the order above (names compared case-insensitively)
- a name written by hand in `try` / `[forced-hosts]` must exist as an environment; the proxy boot test runs with the real names (dummy addresses) and fails otherwise
- creating or editing a server environment changes nothing until the proxy is redeployed

## How a change reaches the servers

1. Edit files under `Backend/` or `Proxy/` and open a PR. CI runs **lint** (every file parses, placeholders are declared, no secret-looking values, no jars/DBs/keys), **gitleaks**, the unit tests, and a **boot test** of every unprotected backend.
2. Merge to `main`. For each affected server the workflow:
   - downloads the server jar and plugin jars **from that server** over SFTP
   - boots them in Docker with the repo configs (rendered with dummy values from `test/test-values.env`) and a throwaway MySQL 8.4.13. It fails if a plugin doesn't enable or an ERROR/stack trace appears that isn't in `test/log-allowlist.yml`.
   - renders the configs with the real secrets and compares them with the server's deploy manifest, which records each file as last deployed. Only files that differ from it are read from the server, drift-checked and uploaded: a commit touching one quest uploads one file, and a changed secret redeploys exactly the files that use it. A manual change on the server to such a file aborts the deploy and prints a diff with the values masked. Server edits to files the commit doesn't touch are left alone; run the workflow manually with **`full_check`** (locally `mc drift <env>`) to compare every file.
   - uploads the changed files atomically (temp files first, then replace). It never deletes anything and never restarts the server.
3. Unprotected environments deploy immediately; protected ones wait for approval in the Actions run.
4. Restart the instance in AMP when convenient.

**First deploy to a new server:** a fresh instance already contains default files (`ops.json`, `spigot.yml`, `config/paper-global.yml`, …). They differ from the repo, so the normal deploy stops on drift. Run **Actions → Minecraft config → Run workflow** with that environment in `targets` and **`adopt`** checked (locally `mc deploy <env> --adopt`). The differing files are replaced and the masked diff is still printed. This only works while the server has no deploy manifest; after the first deploy, drift is always an error again.

If a deploy aborts because of drift, run **Actions → Minecraft sync from server** for that environment. It opens a PR with the server's changes, secrets masked. Merge it (or edit it), and the next deploy goes through.

## Secrets in the workflows

Each job receives only the secrets it needs, listed one by one in its `env` (`NAME: ${{ secrets.NAME }}`); nothing hands out all secrets at once. `mc lint` derives the required list from the schema and the tooling and fails on a missing, extra or renamed entry, or on a secret used anywhere else in a job. A new placeholder therefore also needs its line in the jobs that lint names.

## Placeholders

Write a secret into a config as `"__SECRET_<NAME>__"` (keep the quotes). A whole port number may stay unquoted so it is still a number. `__VAR_<NAME>__` is for values stored as GitHub *variables* (none today); the prefix must match where the value lives. Then:
1. declare it in `secrets.schema.yml` (`kind`, `scope`, `sources`, `description`)
2. add a dummy value to `test/test-values.env`
3. create the real value where its `scope` says: `repository` = once in the repository's Actions secrets; `environment` = in every server environment of those `sources`

A missing or empty value fails the run before anything is uploaded. Environment values override repository values with the same name.

Files that are secrets as a whole (`forwarding.secret`, `floodgate/key.pem`, `votifierplus/rsa/*`, `resourcepackmanager/network-key`) stay on the servers only.

### Knobs (not secret)

`knobs.yml` holds the design and economy knobs, baked in whenever files are rendered (deploy, boot test, `mc render`):
- `__STYLE_<NAME>__`: a color code from `style` (the quests use PRIMARY, SECONDARY, TEXT, DARK, DARKEST)
- `__REWARD_<KIND>_<N>__`: base amount N times `rewards.<KIND>.multiplier`, rounded half up (e.g. `moneten give {player} __REWARD_MONEY_750__ -s`)
- `__REWARD_<KIND>_TEXT_<N>__`: the same amount in the kind's display `text` (e.g. `&e2250 Moneten`)

Change a multiplier or color in `knobs.yml`, and the next deploy uploads every quest that changed. Reload Quests in game afterwards (`/q a reload`). ItsMyConfig is not installed: lint rejects `%itsmyconfig_…%`, `<p:…>` and `"$<…` in configs.

### Server name

`__SERVER_NAME__` is the `<Name>` of the backend's `mc-backend-<Name>` environment, the same name velocity.toml lists it under. Use it wherever a plugin needs an id that differs on every backend: nightcore's and ExcellentEconomy's `MySQL.ServerId`, HuskHomes' `server.yml`, LuckPerms' `server`. It is not secret, needs no schema entry or test value, and may stay unquoted (names are letters, digits, `_` and `-`). Backend only: lint rejects it under `Proxy/`.

## Local use (Windows, Docker Desktop running)

`mc.cmd` (cmd) and `mc.ps1` (PowerShell) are equivalent; use `mc.cmd` where PowerShell scripts are disabled. Both run the tools image, so the commands are identical.

```powershell
.\mc.ps1 lint
.\mc.ps1 unit-tests
.\mc.ps1 install-hooks                              # pre-commit: gitleaks + lint
.\mc.ps1 sftp-probe --host-key-only                 # needs MC_SERVER_HOST + MC_SERVER_SFTP_PORT
.\mc.ps1 test mc-backend-Test --jars-from remote    # needs .mc-local.env, see below
.\mc.ps1 test mc-backend-Test                       # reuse jars cached in .cache/jars
.\mc.ps1 drift mc-backend-Test                      # read-only: what would a deploy change?
```

`.mc-local.env` (git-ignored, repo root) holds the secrets of the server you work with (`MC_SERVER_HOST`, `MC_SERVER_SFTP_PORT`, `AMP_SFTP_HOST_KEY`) and the deploy account (`AMP_SFTP_USER`, `AMP_SFTP_PASSWORD`) as `KEY=value` lines.

`MC_NO_GIT=1` makes the tooling list files from the root `.gitignore` itself instead of calling git, e.g. while you are curating the index by hand. Deploys refuse to run in that mode, because they must prove the files are committed.

Workflows can be exercised with [act](https://nektosact.com). The server-entry hand-over needs act's artifact server:

```bash
act pull_request -j validate
act workflow_dispatch --secret-file .mc-local.env --artifact-server-path .cache/act-artifacts
```

Under act, deploys always run as a dry run unless `--var MC_ACT_ALLOW_DEPLOY=true` is passed.

## What is tracked

Everything under `Backend/` and `Proxy/` is tracked unless `/.gitignore` excludes it. Generated or plugin-installed content (content packages, models, caches, data, jars, DBs, logs, keys) is excluded. To start tracking an excluded file you edit by hand, add a `!` rule for it and run `mc lint`.
