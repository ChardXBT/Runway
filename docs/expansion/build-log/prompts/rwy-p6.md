Job name: **rwy-p6**

Job rwy-p6: build phase P6 ("Private instance data") of the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-p6`, branch
`expansion/k-p6`. It already has a `.venv` with the project installed in editable mode.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read, in your worktree: `docs\expansion\specs\00-principles.md`,
`docs\expansion\specs\07-private-instance-data.md` (all of it),
`docs\expansion\research\private-data-on-public-repo.md`, `docs\expansion\IMPLEMENTATION-PLAN.md`
(phase P6), `AGENTS.md`, `src\runway\operations\database_release.py`,
`scripts\sync_private_database_release.ps1`, `src\runway\config\settings.py`,
`src\runway\platforms\tokens.py`, and `docs\INTELLIGENCE_OPERATIONS.md` (online backup procedure).

Another job is changing `settings.py`, `cli\main.py`, `platforms\sync.py` and the launcher at the
same time. Keep your edits to those files small and self-contained so the merge is easy: new CLI
groups go in their own module, registered in `cli\main.py` with one `add_typer` line; settings
changes are a few new fields plus one loader hook.

## Work packages (all of them)

**6.1 Instance directory and settings load order.** Setting `RUNWAY_INSTANCE_DIR` (unset by
default; when unset, behaviour is exactly today's). When set: load order is process environment,
then `instance.toml` (non-secret settings), then the decrypted `secrets.enc.env` (SOPS + age),
decrypted in memory at start and never written to disk. Point the token store at
`<instance>\tokens\platform-tokens.json` when the instance dir is set. Decryption calls the `sops`
binary via `subprocess` with no shell (`sops --decrypt --input-type dotenv --output-type dotenv
<file>` reading stdout); if `sops` is not installed, fail with a clear message naming what to
install. Do not add a Python dependency for this. Tests use a fake `sops` (a small script on a temp
PATH, or an injectable runner) - never real keys.

**6.2 `runway instance` commands** in `src\runway\operations\instance.py` + a CLI module:
`init --repo <owner/name>`, `status`, `push`, `pull`, `handover`, with every rule in spec 07 §4:
visibility guard (repository must be private, checked with `gh repo view <repo> --json
visibility,nameWithOwner`; refuse if it equals the code repository `ChardXBT/Runway` or any public
repo), lease (`lease.json` with machine id, holder since, heartbeat; stale after 24 h, `--take` to
take a stale one; a machine that does not hold the lease runs in "standby": expose
`instance_is_active(settings) -> bool` and a `StandbyError`, and enforce it at the token store's
refresh/write entry point; the worker will call the same function later), push/handover wait for
an idle worker (expose a hook; for now check a `data\runtime\worker.busy` marker file if present),
pull refuses when another machine holds a fresh lease and when the snapshot's alembic revision is
newer than the checkout, manifest with code commit, alembic revision, file hashes and sizes, SQLite
online backup for the database snapshot, gzip, upload/download as release assets with `gh release`
(tag `snapshot-latest`). No secrets on command lines or in logs. ALL tests mock `gh` and git
remotes (an injectable runner); no test may call the real `gh` against a real repository, create a
repository, or upload anything. Add `-Repository` to `scripts\sync_private_database_release.ps1`
defaulting to the private instance repository named in a setting, keeping its refusal for public
targets; update its tests.

**6.3 Guard rails in the public repository.** `scripts\secret_scan.py` (stdlib only) that fails on
staged or given files matching `*.db`, `*.key`, `.env*` (except `.env.example`),
`platform-tokens.json`, `secrets.enc.*`, `lease.json`, and on token-shaped strings (`ya29.`,
`EAAC`, TikTok `act.` access tokens, `client_secret=` with a value, PEM private key headers,
`rwy_live_` API keys). Avoid false positives on this repository's own docs and tests: run it over
the whole tracked tree and fix the patterns (or add a narrow, reviewed allow-list file) until the
current tree passes. Add a `.pre-commit-config.yaml` local hook that runs it, and a step in
`.github\workflows\ci.yml` (backend job, before tests) that runs it over the tracked tree. A test
proves a fake token file and a fake `ya29.` string are caught.

Also write `docs\PRIVATE_INSTANCE.md`: a short owner guide (create the private repo, install `sops`
and `age`, `runway instance init`, connect accounts, push, second machine, handover). Mark every
step that is the owner's action.

## Done when

All three packages are built with tests; `ruff check .`, `ruff format --check .`, `mypy src/runway`
are clean; the full backend suite passes once under the heavy lock; the acceptance criteria in
spec 07 §9 that can be tested offline are covered by tests (init against a public repo refuses;
push then pull with a fake `gh` and a fixture database yields a working instance; a non-holder
cannot refresh tokens; the secret scan fails a fake token file).

Commit in logical steps on `expansion/k-p6`. Write the report as COMMON-RULES says, at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-p6\REPORT.md`.
