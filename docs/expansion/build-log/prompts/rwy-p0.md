Job name: **rwy-p0**

Job rwy-p0: build phase P0 ("Foundations") of the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-p0`, branch
`expansion/k-p0`. It already has a `.venv` with the project installed in editable mode.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read, in your worktree: `docs\expansion\specs\00-principles.md`,
`docs\expansion\IMPLEMENTATION-PLAN.md` (phase P0), `docs\expansion\01-AUDIT.md` §4 (findings
C2, C3, C7, C8, C9), `docs\expansion\specs\01-dual-use-and-tenancy.md` (§2, §3, §4, §6),
`docs\expansion\specs\03-publishing-engine.md` (§2, §3), `docs\expansion\specs\04-realtime-sync.md`
§3 ("Fix first"), and `IMPLICIT-STATE-MACHINES.md`.

Note: `origin/main` gained fixes on 2026-11-17 and 2026-11-21 (e.g. "Refuse to sync a disconnected
account instead of marking it needs_reauth", "Fix creator-platform token, sync, draft, and analytics
defects"). The audit line numbers are from an older commit. Check the current code before changing
anything; do not redo a fix that is already there, and say in the report which findings were
already fixed.

## Work packages (all of them)

**0.1 No migrations at import.** `create_app()` and importing `runway.api.app` /
`apps\api\main.py` must not run Alembic migrations or create/alter the database. Keep
`app = create_app()` in `apps\api\main.py` working for the launcher. Add `runway db upgrade`
(Typer command; put new CLI groups in their own module and register them in `cli\main.py` with one
line) that upgrades the configured database to head, and `runway db current`. Make the API refuse
to start (clear error, non-zero exit) when the database is behind head, instead of migrating it.
Update `run-runway.ps1` so it runs `runway db upgrade` with the venv Python before starting the
API, and update `tests\unit\test_windows_launcher.py` accordingly. Tests that need a database must
keep working through the existing `initialize_database` fixtures.

**0.2 Profiles and Principal.** Add `RUNWAY_PROFILE` (`local` default, `hosted`) to `Settings` with
the startup validation rules in spec 01 §2 (hosted + SQLite refused; hosted without an identity
provider configured refused - add a setting `identity_provider` that is empty by default; hosted +
`publishing_mode=authorized_browser` refused; local + non-loopback host refused as today). Add the
`Principal` model and scopes/roles from spec 01 §3 in a new module (e.g. `runway\auth\principal.py`),
`require(principal, scope)` raising a 403 problem with code `scope_missing`, and a FastAPI dependency
`get_principal` that in `local` returns the built-in owner (kind `user`, id `local-owner`,
workspace 1, role owner, all scopes). Attach `get_principal` to EVERY route of every router
(router-level dependencies are fine). In `hosted` profile with no identity provider implemented
yet, `get_principal` returns 401 (the identity provider seam comes in a later job). Add a
route-walk test that fails if any API route lacks the dependency (exempt only `/health` and
`/api/health`-style health endpoints and static mounts, and list the exemptions explicitly).

**0.3 Token refresh.** Fix C2 and C3 as spec 04 §3 "Fix first" says: retryable refresh failures
stay retryable and leave the account `connected`; only `invalid_grant`, a 401 from the token
endpoint, or explicit revocation marks `needs_reauth`. Serialize refresh per account across
processes (file lock per account for the file token store; write the new token set before
releasing the lock; re-read the token after acquiring the lock so a second waiter does not refresh
again). Use `expires_in` / `refresh_expires_in` from each response; never a lifetime constant.
Tests: a transient DNS/connect failure leaves the account connected; two concurrent refreshes of a
rotating refresh token make exactly one refresh call (use threads or processes against a fake
token endpoint).

**0.4 Declared state machines.** In `runway\domain\state_machine.py` style, declare machines for
content-draft status, draft-target status and comment-reply/platform-action status covering the
vocabularies stored today (keep stored values unchanged; no data migration of status values).
Route every status write through the machine; remove free-string writes; invalid transitions
raise. Design the draft-target machine so the later publishing job can extend it to the spec 03 §2
vocabulary (draft, ready, released, queued, sending, processing, published, verified, needs_user,
failed_retryable, failed_final, uncertain, cancelled) - if the existing vocabulary maps cleanly onto
a subset of those names, prefer the spec names for new code and document the mapping.

**0.5 Workspaces.** Alembic revision `0013_workspaces` (down_revision = the current head,
`0012_creator_platforms`): tables `workspaces`, `users`, `workspace_members`, `api_keys`,
`audit_log` as in spec 01 §4 (you may instead extend the existing `audit_events` table if that is
cleaner - say which), and insert workspace 1 named after the configured channel name. Revision
`0014_workspace_columns`: add `workspace_id INTEGER NOT NULL DEFAULT 1` with a foreign key to
`workspaces` on every platform table listed in spec 01 §4 (and `channels`), backfill 1, and change
`uq_connected_account_external` to `(workspace_id, platform, external_account_id)`. Use Alembic batch
mode for SQLite. Each revision has a tested downgrade. Update the SQLAlchemy models and
`db\schema_contract.py` / its tests. No isolation enforcement yet (that is phase P7); new rows get
workspace 1.

**0.6 Jobs and worker.** Revision `0015_publish_jobs` (down_revision `0014_workspace_columns`):
table `publish_jobs` exactly as spec 03 §1 (`id, workspace_id, target_id (nullable for
non-publishing kinds), kind, run_at, state, attempts, max_attempts, locked_by, locked_until,
last_error, idempotency_key unique, payload_json, created_at, updated_at`). Add a job module (e.g.
`runway\jobs\`) with enqueue (idempotent on `idempotency_key`), claim (SQLite: `BEGIN IMMEDIATE`,
one worker; leave a clearly marked Postgres `FOR UPDATE SKIP LOCKED` branch with a unit test of the
SQL it would issue), lease, complete, fail with bounded retry/backoff, and the rule that an expired
lease on a `send` job is NOT blindly re-run (hand it to a `reconcile` handler hook; the publishing
job will implement it). Add `runway worker` (Typer) that runs the loop, sleeps until the earliest
`run_at` capped at 30 s, and handles SIGINT/SIGTERM cleanly. Move platform sync onto it:
`PlatformSyncScheduler` stops being a daemon thread in the API process and becomes a producer of
`sync_account` jobs; the worker executes them with the existing sync code. Keep a setting to keep
the old in-process behaviour OFF by default only if removing it would break an existing test that
cannot be updated - prefer removing it. Update `run-runway.ps1` to start and stop the worker
together with the API (same lock, logs under `data\runtime\worker.log`), keep the existing health
checks unchanged, and update the launcher tests.

## Done when

All six packages are built with tests; `ruff check .`, `ruff format --check .`,
`mypy src/runway` are clean; the full backend suite passes once under the heavy lock; migrations
`0013`-`0015` upgrade and downgrade cleanly on a fresh temporary SQLite database AND on a temporary
database first built at `0012_creator_platforms` with a few fixture rows in each platform table
(row counts identical before and after). Do NOT use any production database for this.

Commit in logical steps on `expansion/k-p0`. Write the report as COMMON-RULES says, at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-p0\REPORT.md`, including the final
migration head id and a list of every route file you changed.
