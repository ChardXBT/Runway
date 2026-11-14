# Spec 07 — Private instance data for a public code repository

Decision D9. Research:
[`../research/private-data-on-public-repo.md`](../research/private-data-on-public-repo.md).

## 1. Goal

The owner connects personal YouTube, Instagram and TikTok accounts to their own Runway, keeps
that instance's state versioned and restorable, can bring up a second machine, and nothing
private ever enters the public `ChardXBT/Runway` repository, encrypted or not.

## 2. Design

Two repositories:

| Repository | Visibility | Holds |
| --- | --- | --- |
| `ChardXBT/Runway` | public | Code, migrations, tests, docs. Unchanged rules from `AGENTS.md`. |
| `ChardXBT/runway-instance` (name is a suggestion) | **private** | This instance's settings, encrypted secrets, a manifest, and — as release assets — database and media snapshots |

Layout of the private repository:

```
runway-instance/
  README.md                 what this is; restore steps
  instance.toml             non-secret settings: profile, channel name, time zone, ports, modes
  secrets.enc.env           SOPS + age encrypted: platform client ids/secrets, token key
  tokens/platform-tokens.json   the token store exactly as Runway writes it (already
                                Fernet-encrypted); small; committed
  lease.json                which machine is active, since when, with a heartbeat time
  manifest.json             last snapshot: code commit, alembic revision, file hashes, sizes
  .sops.yaml                age recipients (one per trusted machine)
  .gitattributes / .gitignore   refuse *.db, *.key, .env in plain form
Releases:
  snapshot-latest           runway-database.db.gz, runway-operational-media.zip, manifest
  snapshot-YYYYMMDD…        optional dated copies; keep the newest N
```

On disk the private clone sits **outside** the public checkout, for example
`C:\Users\<user>\runway-instance`, and Runway is pointed at it:

```
RUNWAY_INSTANCE_DIR=C:\Users\<user>\runway-instance
```

Why outside and not a submodule or a nested ignored folder: a submodule records the private
repository's URL and commit ids in the public repository, and a nested clone is one
`git add -f` away from an accident. A sibling directory has no path into the public history.

## 3. What is encrypted with what

| Item | Protection | Why |
| --- | --- | --- |
| Platform client secrets, token key | `age` via SOPS, in `secrets.enc.env` | A leaked GitHub token or an accidental collaborator must not yield usable secrets |
| Platform tokens file | Fernet (existing), key inside `secrets.enc.env` | Already ciphertext; committing it to a private repo adds versioning without new exposure |
| Database snapshot | Compressed; stored as a private release asset | Too large for git; contains no tokens (they live in the token store) |
| Media snapshot | Zip as a private release asset | Same |
| Browser profile, plain `token.key`, `.env` | **Never stored** | A signed-in Google session must not travel; plain keys defeat the encryption |

`age` private keys live in each machine's user profile (or a password manager), never in either
repository. Adding a machine means adding its public key to `.sops.yaml` and re-encrypting.
Removing a machine means removing its key, re-encrypting, **and rotating the secrets**, because
the removed machine has seen them.

Optional hardening: encrypt the database snapshot with `age` before upload. Recommended if the
private repository ever gains a second collaborator.

## 4. Commands

New CLI group `runway instance`, implemented in `src/runway/operations/` beside the existing
`database_release.py`:

| Command | Does |
| --- | --- |
| `runway instance init --repo <owner/name>` | Verifies with `gh` that the repository is **private**; refuses otherwise. Creates the layout, generates an age key if none, writes `.sops.yaml`. |
| `runway instance status` | Shows lease holder, last snapshot, drift between local state and the manifest. |
| `runway instance push` | Takes the lease; SQLite online backup (existing procedure); compress; upload assets; write manifest; commit small files; push. |
| `runway instance pull` | Refuses if another machine holds a fresh lease; downloads and verifies hashes; restores to the data directory; checks the alembic revision against the code checkout. |
| `runway instance handover` | Push, then release the lease, so another machine can pull. |

Rules built into the commands:

- **Visibility guard.** Every command that uploads first checks the target repository is
  private (the same guard the existing script has, pointed at the new repository). It also
  refuses if the target equals the code repository.
- **Lease.** `lease.json` names the active machine with a heartbeat. A machine that is not the
  holder starts Runway in read-only "standby" for platform writes and token refresh. A stale
  lease (older than 24 hours) can be taken with `--take`. Reason: TikTok rotates refresh tokens,
  and the database is single-writer; two active machines corrupt both.
- **Never while running a publish.** `push` and `handover` wait for the worker to be idle.
- **Manifest check on pull.** If the snapshot's alembic revision is newer than the checkout,
  stop and say "update the code first".
- **No secrets on the command line or in logs.**

The existing `scripts/sync_private_database_release.ps1` keeps working; give it a
`-Repository` parameter defaulting to the private instance repository, and keep its refusal for
public targets.

## 5. Connecting personal accounts with this in place

1. Create developer apps (runbook in spec 09). Put client ids and secrets in
   `secrets.enc.env`.
2. Runway loads settings in this order: process environment, then `instance.toml`, then the
   decrypted `secrets.enc.env` (decrypted in memory at start, never written to disk).
3. Connect each account in the Accounts page as today. Tokens land in the token store inside
   the instance directory.
4. `runway instance push`. The connection is now versioned and restorable.
5. On a second machine: clone both repositories, install the age key, `runway instance pull`,
   then `runway instance handover` back and forth as needed.

## 6. Guard rails in the public repository

- Keep the ignore rules. Add a pre-commit hook and a CI job that fail on: files matching
  `*.db`, `*.key`, `.env*` (except `.env.example`), `platform-tokens.json`, `secrets.enc.*`,
  `lease.json`; and on token-shaped strings (the patterns already used in
  `docs/VERIFICATION_REPORT.md`: `ya29.`, `EAAC`, `act.`, `client_secret=`, private key
  headers).
- Tell any automation that scans the owner's repositories (code-review bots, the Supervisor's
  auto-update) to skip `runway-instance`.

## 7. Why not the alternatives (summary)

- **git-crypt or SOPS in the public repo:** publishes ciphertext of live channel credentials
  permanently; no revocation; a single key leak exposes all history; the database does not fit.
- **Private fork:** GitHub does not allow it for a public repository; a private mirror means two
  drifting codebases.
- **Make the code private:** works, and remains the simplest fallback if the public showcase
  stops mattering. It does not by itself provide the sync workflow above.

## 8. Relationship to hosted mode

When a hosted Runway exists, the owner's platform data (accounts, posts, inbox, analytics) can
live in a hosted workspace and needs no sync. The intelligence database and the browser
publisher stay local, so this spec remains the way to protect and move them.

## 9. Acceptance criteria

- `runway instance init` against a public repository refuses with a clear message.
- A push followed by a pull on a clean machine yields a working instance with accounts still
  connected (tested with mock providers and a fixture database).
- With machine A holding the lease, machine B cannot refresh tokens or run the worker.
- The CI secret scan fails a test commit containing a fake token file.
- No plain secret exists on disk after startup other than in process memory and the token store.
