# Private instance data

The Runway code repository is public. Your instance's state (settings, platform client secrets,
the token store, database and media snapshots) lives in a **separate private repository** that
you own, cloned **outside** the code checkout. Design: [spec 07](expansion/specs/07-private-instance-data.md).

Steps marked **[You]** are yours to do; Runway never creates repositories, installs tools or
signs in for you. Everything else is a `runway instance` command.

| In the private repository | Protection |
| --- | --- |
| `instance.toml`: shared non-secret settings (allow-list) | none needed |
| `secrets.enc.env`: client ids and secrets, the token key | SOPS + age |
| `tokens/platform-tokens.json` | Fernet, key inside `secrets.enc.env` |
| `lease.json`, `manifest.json` | none needed |
| Releases `snapshot-YYYYMMDD-HHMMSS` (newest 5 kept) and `snapshot-latest`: database and media | private release assets |

The browser profile, plain key files and `.env` never travel. Sign in to the Community-post
publisher separately on each machine.

## 1. Create the private repository [You]

```powershell
gh auth login
gh repo create <owner>/runway-instance --private --add-readme
```

Every command that uploads checks with `gh repo view` that this repository is private and is
not `ChardXBT/Runway`. A public target is always refused. Git then pushes only to
`https://github.com/<owner>/runway-instance.git` by name (using `gh` for credentials), and
refuses when the clone's push URL, an SSH host alias, or a `url.*.insteadOf` rule would send the
data anywhere else.

Tell any automation that scans your repositories (code-review bots, the Supervisor's
auto-update) to skip `runway-instance`. **[You]**

## 2. Install SOPS and age [You]

```powershell
winget install --id SecretsOPerationS.SOPS
winget install --id FiloSottile.age
```

Open a new terminal afterwards so both are on `PATH`. When `RUNWAY_INSTANCE_DIR` is set and
`sops` is missing, Runway stops at startup and says what to install.

## 3. Initialize this machine

Pick a directory outside the code checkout (Runway refuses one inside it):

```powershell
.\.venv\Scripts\runway.exe instance init --repo <owner>/runway-instance --dir C:\Users\<you>\runway-instance
```

`init`:

- clones the repository and writes the layout, `.sops.yaml` and `lease.json` (this machine
  becomes the active one);
- creates an age key in `%APPDATA%\sops\age\keys.txt` if there is none;
- copies an existing `data\secrets\token.key` and token store into the instance, encrypting the
  key in `secrets.enc.env` together with any platform client ids and secrets from your settings
  (the originals stay where they are until you delete them);
- commits and pushes the small files.

Then, **[You]**:

1. Set `RUNWAY_INSTANCE_DIR=C:\Users\<you>\runway-instance` in the checkout's `.env` (or the
   user environment). The machine's lease identity is created once in
   `%LOCALAPPDATA%\Runway\machine-id` (computer name plus a random suffix); `RUNWAY_MACHINE_ID`
   overrides it. Never set it in `instance.toml` or `secrets.enc.env`: Runway refuses that,
   because every machine would claim the same identity.
2. Copy the age key file into your password manager. Without it, nobody can decrypt the
   secrets.
3. Remove the platform client secrets from the checkout's `.env`, start Runway once to confirm
   the accounts still work, then delete `data\secrets\token.key`. `runway instance status`
   warns while that plain key exists.

Load order at startup: process environment, then the checkout's `.env`, then `instance.toml`,
then `secrets.enc.env`, which is decrypted in memory and never written to disk.

## 4. Add or change secrets [You]

```powershell
cd C:\Users\<you>\runway-instance
$env:SOPS_EDITOR = "notepad"
sops edit secrets.enc.env
```

Use `RUNWAY_` names, for example `RUNWAY_YOUTUBE_CLIENT_SECRET=...`. The file accepts platform
client ids and secrets, API keys and `RUNWAY_TOKEN_KEY` only.

`instance.toml` accepts only shared, non-secret settings: product and channel names, time zone,
post time and cadence, ports, `platform_provider`, `publishing_mode`, sync intervals,
`instance_repository` and `instance_snapshot_keep`. Runway refuses to start if it contains a
secret, or a machine-local setting such as `machine_id`, `data_dir`, `codex_cli_path`,
`publisher_chrome_path`, `agent_runtime` or `oauth_redirect_base_url`.

## 5. Connect accounts and push

Connect each account in the Accounts page as usual **[You]**. Tokens land in
`<instance>\tokens\`. Then:

```powershell
.\.venv\Scripts\runway.exe instance push
```

`push` waits for a running publish or comment reply to finish, takes an online SQLite backup,
writes `manifest.json` (code commit, migration, hashes, sizes), and commits and pushes it with
the renewed lease. Only when that push wins does it upload the compressed database and media to
a new `snapshot-YYYYMMDD-HHMMSS` release, copy them to `snapshot-latest`, and delete dated
releases beyond the newest `instance_snapshot_keep` (default 5). `runway instance status` shows
the lease, the last snapshot and what changed since.

## 6. Add a second machine

1. **[You]** On machine B: clone the code repository, create its `.venv`, install SOPS and age,
   set `RUNWAY_INSTANCE_DIR`.
2. On machine B: `runway instance init --repo <owner>/runway-instance`. It clones the instance,
   adds B's age public key to `.sops.yaml` and pushes. B starts in **standby**.
3. **[You]** On machine A, re-encrypt for both machines:
   ```powershell
   git -C C:\Users\<you>\runway-instance pull --ff-only
   sops updatekeys C:\Users\<you>\runway-instance\secrets.enc.env
   .\.venv\Scripts\runway.exe instance push
   ```

## 7. Hand over between machines

Only the machine holding the lease may refresh tokens or publish. TikTok rotates refresh tokens
and the database is single-writer, so two active machines would break each other.

```powershell
# On the active machine
.\.venv\Scripts\runway.exe instance handover
# On the other machine
.\.venv\Scripts\runway.exe instance pull
```

`pull` refuses while another machine holds a fresh lease and refuses a snapshot newer than your
code checkout ("update the code first"). It downloads and verifies every hash first, takes the
lease only then, and backs up any database it replaces under `data\backups\`. On the machine
that already holds the lease, it refuses to replace a local database that changed after the
snapshot unless you pass `--force` **[You decide]**.

In standby, token refresh, connecting and disconnecting accounts, comment replies, YouTube
scheduling and the publisher worker all fail with a clear "standby" error (HTTP 409); accounts
stay connected and read-only syncs continue.

While Runway runs, the active machine renews the lease heartbeat in the private repository about
every five hours (`runway instance heartbeat` does it by hand). Every machine re-reads the
remote lease at least every 10 minutes before a token refresh or platform write, so a machine
whose lease was taken drops to standby on its own. If the remote cannot be reached, the machine
stays in standby until it can. A lease whose remote heartbeat is older than 24 hours is stale:
if the active machine is lost, take it over with `runway instance pull --take` **[You decide]**.

## 8. Remove a machine [You]

Remove its key from `.sops.yaml`, run `sops updatekeys secrets.enc.env`, **rotate every platform
client secret and the token key** (the removed machine has seen them), and push.

## Guard rails in the public repository

`scripts/secret_scan.py` refuses databases and snapshots (`*.db`, `*.db.gz`, `*.db-wal`,
`*.db-shm`, `*.sqlite*`, `runway-operational-media.zip`), `*.key`, `.env*` (except
`.env.example`), `platform-tokens.json`, `secrets.enc.*`, `lease.json`, and token-shaped strings
(Google, Meta, Instagram, TikTok and GitHub tokens, client secrets, token keys, private keys) in
UTF-8 and UTF-16 files. CI scans every commit of each push or pull request, plus the final tree.

CI runs after the push, when the commit is already public. To stop a leak before it leaves your
machine:

1. **[You]** Enable the hook in each clone of the public repository:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_hooks.ps1
   ```
   It sets `core.hooksPath` to `.githooks`, whose `pre-commit` scans the staged content. (The
   `pre-commit` framework works too: `pip install pre-commit`, then `pre-commit install`.)
2. **[You]** In the public repository's GitHub settings, under *Code security*, enable **Secret
   scanning** and **Push protection**, so GitHub itself rejects a push containing a recognized
   credential.

`scripts/sync_private_database_release.ps1` still exists. It now targets `-Repository`, else the
instance repository from your settings, and still refuses any repository that is not private.
