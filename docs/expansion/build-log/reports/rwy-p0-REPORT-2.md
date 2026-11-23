Done: P0 work packages 0.1–0.7 are built, tested and committed on `expansion/k-p0`; final migration head is `0016_shared_contracts`; the full suite was not run locally (rule 7, CI runs it).

# rwy-p0 report: phase P0 "Foundations" plus 0.7 shared v1 contracts

Worktree `%USERPROFILE%\Downloads\RunWay-k-p0`, branch `expansion/k-p0`, base `f0c136f`. Nothing pushed.

## Commits

| Hash | Subject |
| --- | --- |
| `030ecab` | feat(db): stop migrating at import; add runway db upgrade/current and refuse a stale database |
| `3b7c407` | feat(auth): add RUNWAY_PROFILE validation, Principal, require() and get_principal on every route |
| `b1a169d` | fix(platforms): keep transient refresh failures retryable and serialize refresh per account |
| `e8825a2` | feat(domain): declare draft, target, comment and reply-action state machines |
| `093e07a` | feat(db): add workspaces (0013), workspace_id on platform tables (0014) and publish_jobs (0015) |
| `6ecb690` | feat(jobs): add the publish_jobs queue and runway worker; move scheduled sync onto it |
| `3507630` | fix(api): serve the local OpenAPI docs behind get_principal so no route is exempt beyond /health and /media |
| `404529e` | feat(api): add shared /api/v1 contracts, idempotency keys, event log and SSE stream (0016) |

## Audit findings: already fixed vs fixed here

- **C2 (transient refresh failure → needs_reauth): already partly fixed on main.** `ProviderRegistry.fresh_token` and `SyncService._execute` already kept a plain `ProviderError` from marking `needs_reauth`. The existing test `test_transient_refresh_failure_fails_the_run_but_keeps_the_account_connected` covered that. I did not redo it. I closed two remaining gaps:
  - TikTok's token endpoint returns RFC 6749 string errors (`{"error": "invalid_grant"}`), which fell through to the generic branch. They are now classified: `invalid_grant` or a 401 means needs_reauth; a 5xx or 429 is retryable.
  - Instagram's "200 without a token" was `AuthorizationExpired`. It is now a retryable `ProviderError`.
- **"Refuse to sync a disconnected account" (16bbcf0): already on main; untouched.**
- **C3 (refresh not serialized): fixed here** (0.3).
- **C7 (migrations at import): fixed here** (0.1).
- **C8 (sync thread inside the API process): fixed here** (0.6).
- **C9 (free-string draft and target status): fixed here** (0.4).

## What each package does

### 0.1: no migrations at import
- `create_app()` builds `Database(settings)` only. It never runs Alembic or opens the database. `app = create_app()` in `runway.api.app` and `apps/api/main.py` still works.
- The API lifespan calls `require_database_at_head`. A database that is missing, behind head, or at a revision this code does not know raises `DatabaseNotCurrent`. The message names `runway db upgrade`, and uvicorn exits non-zero. This is tested with a real uvicorn subprocess.
- `runway db upgrade` and `runway db current` live in `src/runway/cli/db_commands.py`, registered with one line in `cli/main.py`. `db current` is read-only: it opens SQLite with `mode=ro` and never creates the file.
- `run-runway.ps1` gained `Invoke-DatabaseUpgrade`. It runs `<venv python> -m runway.cli.main db upgrade` just before starting the API, with output in `data\runtime\db-upgrade.log`.
  - It waits up to 15 minutes and never kills a running migration.
  - A non-zero exit aborts the launch.
- `run-runway.sh` also runs `db upgrade` and starts the worker.
- Tests that need a database go through `initialize_database` (the existing `database` fixture or a small `migrated()` helper).

### 0.2: profiles and Principal
- `Settings.profile` (`RUNWAY_PROFILE`, default `local`) and `Settings.identity_provider` (default empty).
- A model validator refuses:
  - hosted + SQLite;
  - hosted without an identity provider;
  - hosted + `authorized_browser`;
  - local + a non-loopback host (unchanged message).
- `src/runway/auth/principal.py` holds `Principal`, the scope constants, `ROLE_SCOPES` (exactly as spec 01 §3), `local_owner()`, `system_worker()`, `require(principal, scope)` (403 `scope_missing`) and `get_principal`.
  - In local, `get_principal` returns `user / local-owner / workspace 1 / owner / all scopes`.
  - In hosted it returns a 401 `authentication_required` problem.
- `get_principal` is a router-level dependency on all five routers and on `/api/settings`.
- FastAPI's built-in `/openapi.json`, `/docs` and `/redoc` cannot take dependencies, so they were disabled and re-added as ordinary routes behind `get_principal` (local only; hosted serves no docs).
- The route-walk test is `tests/integration/test_route_principal.py`. Its explicit exemptions are `EXEMPT_HEALTH_ROUTES = {"/health"}` and `EXEMPT_STATIC_MOUNTS = {"/media"}`. It also asserts that every `/api/v1` route is a `V1Route`.

### 0.3: token refresh
- `TokenStore.refresh_lock(token_ref)` takes a per-account OS file lock at `secrets/refresh-locks/<token_ref>.lock`, shared across threads and processes (code in `src/runway/locks.py`).
- `ProviderRegistry.fresh_token` does the whole refresh under that lock: re-read the token, refresh only if still needed, store the new set, then release.
- A lock that stays busy past 90 s raises a retryable `ProviderError`, never `AuthorizationExpired`.
- Lifetimes come from each response's `expires_in` / `refresh_expires_in`. Fields a TikTok refresh response omits keep the previous response's values.
- Tests:
  - a DNS `ConnectError` leaves the account `connected`;
  - two threads, each with its own registry and token store, refresh a rotating token against a fake TikTok endpoint and make exactly one call;
  - a separate subprocess holds the lock and excludes the test process.

### 0.4: declared state machines
- `src/runway/domain/state_machine.py` adds a generic `StateMachine[StateT]` with `parse`, `allowed`, `require`, `start` and `apply`. Writing the current value again is allowed.
- Declared machines: `DRAFT_STATUS_MACHINE`, `TARGET_STATE_MACHINE`, `PLATFORM_ACTION_MACHINE`, `COMMENT_STATUS_MACHINE`. The rule for a draft's status is `derive_draft_status`.
- Enums live in `domain/enums.py`: `DraftStatus`, `TargetState`, `PlatformActionState`, `CommentStatus`.
- Stored values are unchanged. `TargetState` uses the spec 03 §2 names. The only renamed value is `TargetState.DRAFT`, stored as the legacy `"pending"`.
- Only today's manual edge (`pending → published`) is enabled. The publishing job adds the other edges.
- Every write in `drafts.py`, `inbox.py` and `sync.py` goes through a machine. An AST test fails on any string-literal status write to these models.
- Behaviour fixes:
  - clearing a schedule returns a draft to `draft`;
  - an explicit status that contradicts the schedule is a 422;
  - publishing a target no longer un-archives an archived draft.

### 0.5: workspaces
- `0013_workspaces` creates `workspaces`, `users`, `workspace_members`, `api_keys` and a **new** `audit_log` table. `audit_events` is unchanged: it serves the activity feed with a different shape. It inserts workspace 1 named after the configured channel name, passed in through `alembic_config()` attributes.
- `0014_workspace_columns` rebuilds, in batch mode, `channels` plus the 14 platform tables. Each gets `workspace_id INTEGER NOT NULL DEFAULT 1`, a foreign key `fk_<table>_workspace` and an index `ix_<table>_workspace_id`. `uq_connected_account_external` becomes `(workspace_id, platform, external_account_id)`.
- Models use a `WorkspaceOwned` mixin. `docs/schema/intelligence-data-flywheel.json` is regenerated; the schema-contract tests check `workspace_id` on every scoped table.

### 0.6: jobs and worker
- `0015_publish_jobs` has exactly the spec columns.
- `src/runway/jobs/queue.py`:
  - `JobQueue.enqueue` is idempotent on `idempotency_key`, including under a concurrent race.
  - `claim` runs under `BEGIN IMMEDIATE` on SQLite. `claim_statement("postgresql", ...)` issues `FOR UPDATE SKIP LOCKED` and has a unit test on the compiled SQL.
  - Also `extend_lease`, `complete`, and `fail` with 1/5/20-minute backoff up to `max_attempts`. `JOB_STATE_MACHINE` covers job states.
  - An expired lease on a `send` job is never re-run. It goes to `expired_send_handler`, which by default marks the send `reconciling` and enqueues a `reconcile` job.
- `src/runway/jobs/worker.py`: `Worker` sleeps until the earliest `run_at` or producer slot, capped at 30 s, and extends a running job's lease with a heartbeat thread.
- `runway worker` (`src/runway/cli/worker_commands.py`):
  - refuses a stale database (exit 1);
  - holds a single-instance lock at `data/runtime/worker.lock` (exit 3 if another worker runs);
  - stops cleanly on SIGINT, SIGTERM and SIGBREAK;
  - has a `--once` option.
- `PlatformSyncScheduler` is now a producer of `sync_account` jobs: one per connected account per interval slot, `max_attempts=1`. `run_sync_account_job` runs the existing `SyncService.run`. The in-process thread is removed and no setting keeps it.
- `run-runway.ps1` gained `Start-Worker`, called right after the API starts, inside the same `launcher.lock`.
  - It logs to `data\runtime\worker.log`.
  - It skips the start if an owned worker is already running.
  - The worker is tracked in `$started`, so a failed launch rolls it back.
  - The health checks are unchanged.

### 0.7: shared v1 contracts
See "Shared contracts" below. In short:
- `build_v1_router` and `V1Route`;
- problem+json with `ProblemCode`, with 422 and 404 converted only under `/api/v1`;
- an opaque keyset cursor;
- If-Match plus an atomic compare-and-set;
- an Idempotency-Key store opted into with `@idempotent`;
- the `events` table with `record_event`;
- `GET /api/v1/events/stream`;
- migration `0016_shared_contracts`;
- an hourly worker `purge` job that enforces the 24 h idempotency and 7-day event retention.

## Shared contracts (for the publishing, real-time and agent jobs)

1. **v1 router factory**: `from runway.api.v1.router import build_v1_router, V1Route, V1_PREFIX`
   - `build_v1_router(*, tags: Sequence[str | Enum], prefix: str = "") -> APIRouter`. The prefix is relative to `/api/v1` (for example `"/posts"`). The router has `dependencies=[Depends(get_principal)]`, `route_class=V1Route` and problem response docs.
   - `V1_PREFIX = "/api/v1"`.
   - Include the router in `create_app` (`src/runway/api/app.py`, next to `build_events_router`). The route-walk test then covers it automatically.
2. **Problems**: `from runway.auth.problems import Problem, ProblemCode, problem_response, PROBLEM_MEDIA_TYPE, default_status`
   - `ProblemCode` members: `SCOPE_MISSING`, `VERSION_CONFLICT`, `HUMAN_REQUIRED`, `CAPABILITY_NOT_READY`, `VALIDATION_FAILED`, `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `RATE_LIMITED`, plus the existing `AUTHENTICATION_REQUIRED` and `API_KEY_UNSUPPORTED`.
   - `Problem.of(code: ProblemCode, detail: str, *, status_code: int | None = None, headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None) -> Problem`. Raise it; `Problem` is an `HTTPException` subclass.
   - Handlers: `from runway.api.v1.errors import install_problem_handlers` (already called in `create_app`). `Problem` renders as `application/problem+json` everywhere. A `RequestValidationError` or 404 under `/api/v1` becomes `validation_failed` or `not_found`, with `errors` reduced to `loc`, `msg` and `type`. Unversioned routes keep FastAPI's `{"detail": ...}` format.
3. **Pagination**: `from runway.api.v1.pagination import CursorPage, CursorParams, cursor_params, paginate, encode_cursor, decode_cursor`
   - Dependency: `params: CursorParams = Depends(cursor_params)`, with `?cursor=` up to 512 characters and `?limit=` from 1 to 200 (default 50).
   - `paginate(session: Session, statement: Select[tuple[RowT]], key: InstrumentedAttribute[int], params: CursorParams, *, scope: str, descending: bool = True) -> tuple[list[RowT], str | None]`. The statement must already filter by workspace.
   - Response model: `CursorPage[Item]` with `items` and `next_cursor`.
   - A bad, foreign-scope or wrong-order cursor gives 422 `validation_failed`.
4. **If-Match / version**: `from runway.api.v1.concurrency import if_match_version, parse_if_match, require_version, bump_version, etag_for, version_conflict`
   - Dependency: `expected: int = Depends(if_match_version)`. A missing or `*` header gives 428 `validation_failed`.
   - `require_version(current: int, expected: int) -> None` raises 409 `version_conflict` with `current_version` in the body and an `ETag` header.
   - `bump_version(session: Session, table: Table, row_id: int, expected: int, *, version_column: str = "version") -> int` is an atomic compare-and-set UPDATE. It raises `version_conflict` or `not_found`.
   - `etag_for(version: int) -> str` returns `'"<n>"'`.
5. **Idempotency**: `from runway.api.v1.idempotency import idempotent, IdempotencyStore, IDEMPOTENCY_HEADER, REPLAYED_HEADER, IDEMPOTENCY_RETENTION`
   - Opt in by putting `@idempotent` **under** the route decorator on a v1 router: `@router.post(...)` then `@idempotent` then `def handler(...)`. `V1Route` applies it to POST, PUT, PATCH and DELETE requests that carry an `Idempotency-Key` header.
   - Keys are scoped by `(workspace_id, "<kind>:<principal id>", key)`.
   - Same key and same request (method, path, query, canonical JSON body) replays the stored response with `Idempotent-Replayed: true`. A different request gives 409 `idempotency_conflict`. A concurrent duplicate gives 409 with `Retry-After: 1`.
   - Only 2xx responses with a body are stored; a failure releases the key.
   - Model: `runway.db.models.IdempotencyKey`, table `idempotency_keys`.
6. **Events**: `from runway.events.log import record_event, Event, events_after, latest_event_id, resume_is_complete, purge_expired_events, EVENT_RETENTION, MAX_PAYLOAD_BYTES`
   - `record_event(session: Session, workspace_id: int, type: str, subject_type: str, subject_id: str | int | None, payload: dict[str, Any] | None = None) -> EventRecord`. It flushes into the caller's transaction and never commits.
   - Unknown types raise `UnknownEventType`. Payloads over 4096 bytes raise `EventPayloadTooLarge`.
   - Model: `runway.db.models.EventRecord`, table `events`, AUTOINCREMENT ids.
   - **Event-type module: `runway.events.types`**. Constants: `ACCOUNT_UPDATED`, `SYNC_STARTED`, `SYNC_FINISHED`, `CONTENT_IMPORTED`, `COMMENT_NEW`, `TARGET_STATE_CHANGED`, `TARGET_PROGRESS`, `TARGET_AWAITING_RELEASE`, `MEDIA_READY`, `QUOTA_LOW`. The set of all of them is `EVENT_TYPES`; the stream-only control type is `RESYNC = "resync"`. Add a constant there before writing a new type.
7. **SSE stream**: `GET /api/v1/events/stream`, built by `runway.api.v1.events_routes.build_events_router(database: Database, *, poll_seconds: float = 1.0, heartbeat_seconds: float = 20.0) -> APIRouter`. The frame loop is `runway.events.stream.event_stream(database, workspace_id, last_event_id, *, wakeup, is_disconnected, heartbeat_seconds=20.0, clock=time.monotonic)`.
   - The first frame is `retry: 3000`. It is sent after the start position is fixed, so a client can load state once it arrives.
   - Each event frame is `id: <row id>`, `event: <type>`, then `data: {id, type, subject_type, subject_id, payload, created_at}`.
   - Resume uses the `Last-Event-ID` header, or the `?last_event_id=` query for a first connection.
   - If that id is unknown, belongs to another workspace, was purged, or is older than 7 days, the stream sends `event: resync` with `{"reason", "resume_from"}` first.
   - A `: heartbeat` comment goes out every 20 s.
   - Wake-up is `wakeup_for(database)`, which returns `PollingWakeup` (1 s poll). `PostgresNotifyWakeup` is the clearly marked LISTEN/NOTIFY hook; it raises `NotImplementedError` and is not selected yet.
   - The stream checks no scope; any principal sees only its own workspace's events.

**Final migration head: `0016_shared_contracts`.**

## Migrations

| Revision | down_revision | Content |
| --- | --- | --- |
| `0013_workspaces` | `0012_creator_platforms` | workspaces, users, workspace_members, api_keys, audit_log; workspace 1 |
| `0014_workspace_columns` | `0013_workspaces` | `workspace_id` on channels + 14 platform tables; per-workspace account uniqueness |
| `0015_publish_jobs` | `0014_workspace_columns` | job queue table |
| `0016_shared_contracts` | `0015_publish_jobs` | `idempotency_keys`, `events` |

Each revision skips work that is already done. This is needed because `0001_initial` runs `create_all` from the current models, so a fresh database already has the new tables and columns.

Rehearsals were run on temporary SQLite databases only; I opened no production database. Covered in `tests/integration/test_workspace_migrations.py`:
- **Seeded 0012 database:** rows in every platform table, then upgrade to head and downgrade back. Row counts are identical, ids and statuses survive the rebuilds, and `PRAGMA foreign_key_check` is clean.
- **Downgraded schema:** it equals the committed 0012 fingerprint `b70dadf3…`.
- **Step by step:** each revision goes up and down one at a time with counts compared.
- **0016 alone:** it goes up and down with the 0015 fingerprint restored.
- **Fresh database:** upgrade, downgrade, re-upgrade gives an identical fingerprint. AUTOINCREMENT on `events` is present on both the fresh and the migrated path.

## Route files changed
- `src/runway/api/catalog_routes.py`, `discovery_routes.py`, `intelligence_routes.py`, `platform_routes.py` and `proposal_routes.py`: router-level `Depends(get_principal)`.
- `platform_routes.py` also: `drafts_update` maps state-machine `ValueError` to 422.
- `src/runway/api/app.py`: principal on `/api/settings`, guarded docs routes, problem handlers, v1 events router.
- New: `src/runway/api/v1/events_routes.py`.

## Files added or changed
76 files, +7653/−240 against `f0c136f` (`git diff --name-status f0c136f..HEAD`).
- **Added source:**
  - `src/runway/auth/` (`principal.py`, `problems.py`)
  - `src/runway/api/v1/` (`router`, `errors`, `pagination`, `concurrency`, `idempotency`, `events_routes`)
  - `src/runway/events/` (`types`, `log`, `stream`)
  - `src/runway/jobs/` (`queue`, `worker`, `runtime`, `retention`)
  - `src/runway/locks.py`, `src/runway/cli/db_commands.py`, `src/runway/cli/worker_commands.py`
  - `alembic/versions/0013`–`0016`
- **Changed source:**
  - `api/__init__.py`, which is now lazy so importing `runway.api.v1.*` does not build the app
  - `api/app.py` and the five route files
  - `config/settings.py`, `db/bootstrap.py`, `db/models.py`, `domain/enums.py`, `domain/state_machine.py`, `cli/main.py`
  - `platforms/{drafts,inbox,overview,registry,sync,tiktok,instagram,tokens}.py`
  - `run-runway.ps1`, `run-runway.sh`, `.env.example`, `docs/OPERATIONS.md`, `docs/schema/intelligence-data-flywheel.json`
- **Tests added:** `tests/unit/test_{principal,token_refresh_serialization,platform_state_machines,job_queue,v1_contracts,events}.py` and `tests/integration/test_{database_lifecycle,route_principal,workspace_migrations,worker_process,v1_idempotency,event_stream_http}.py`.
- **Tests changed:** `conftest.py`, `test_windows_launcher.py`, and the integration tests that need a migrated database or the new head: `api_lifecycle_log`, `api_request_guard`, `canonical_migration`, `creator_platforms_migration`, `foundation`, `overview_route`, `platform_regressions`, `platform_routes`, `schema_contract`, `youtube_publisher`.

## Tests run (all local, PowerShell, `.\.venv\Scripts\python.exe -m pytest … -p no:cacheprovider`)

**Final run for 0.7, the rule-7 scope:**
- `pytest tests\unit -q -x`: **444 passed, 1 skipped**, 446 s.
- 15 integration files (`test_v1_idempotency`, `test_event_stream_http`, `test_route_principal`, `test_workspace_migrations`, `test_schema_contract`, `test_canonical_migration`, `test_creator_platforms_migration`, `test_database_lifecycle`, `test_worker_process`, `test_api_lifecycle_log`, `test_api_request_guard`, `test_foundation`, `test_platform_routes`, `test_overview_route`, `test_platform_regressions`): **94 passed, 1 failed**, 492 s.
  - The failure was `test_foundation.py::test_migrations_seed_channel_and_enable_wal`, which hardcoded the head `0012_creator_platforms`. It had been stale since 0.5, because I had not re-run that file after 0.5. I updated it to `0016_shared_contracts` and the file re-ran at **7 passed**, 30 s.

**Targeted runs while building 0.7:**
- `test_v1_contracts`: 19 passed.
- `test_events` + `test_v1_idempotency`: 20 passed after removing one invalid test case (httpx cannot send a non-ASCII header).
- `test_event_stream_http`: 3 passed, 22 s, against uvicorn on a random free port. The first attempt timed out because the test wrote events before the stream had fixed its start position. The test now waits for the `retry:` frame; the stream code was unchanged apart from a comment.

**Earlier package runs, all passing at the end:**

| Package | Files | Result |
| --- | --- | --- |
| 0.1 | `test_database_lifecycle` | 9 passed, 60 s |
| 0.1 | launcher | 6 passed |
| 0.1 | foundation, request-guard, overview, platform-routes, platform-regressions, lifecycle-log | 49 passed, 270 s |
| 0.2 | principal + route walk | 17 passed |
| 0.2 | `test_youtube_publisher`, `test_platform_routes`, `test_api_request_guard`, `test_settings_platforms` | 81 passed, 1010 s |
| 0.3 | token and provider files | 50 passed |
| 0.4 | state machines, drafts, inbox, sync, sync views, proposal state machine | 60 passed, 156 s |
| 0.5 | workspace migrations | 6 passed, 39 s |
| 0.5 | schema-contract and migration files | 17 passed (after fixing one head assertion) |
| 0.6 | `test_job_queue` | 19 passed |
| 0.6 | `test_worker_process` | 2 passed, 66 s |
| 0.6 | launcher | 8 passed |
| 0.6 | tokens + sync + lifecycle-log + job queue | 50 passed, 142 s |

**Not run:** the full backend suite and `tests/e2e`. Under the new rule 7, CI runs them. One earlier full-suite attempt under the heavy lock was interrupted and produced no result. No heavy lock is held now.

Not re-run after the later packages: integration files outside the list above, for example the intelligence, caption and agent-harness files. They use the `database` fixture, which migrates to head, so CI will be the first to exercise them against the 0.5 model changes.

**Static checks:** `ruff check .` gives "All checks passed!". `ruff format --check .` reports 251 files already formatted. `mypy src/runway` (strict) finds no issues in 148 source files.

## Deviations and decisions
- **audit_log:** a new table rather than new columns on `audit_events`, which keeps its own shape for the activity feed.
- **Hosted profile cannot start yet.** `database_url` is always SQLite (there is no Postgres URL setting yet), so `RUNWAY_PROFILE=hosted` is always refused. This is the safe reading until P7.
- **API keys:** a presented `Authorization: Bearer rwy_…` key gets 401 `api_key_unsupported` in both profiles. It is never silently treated as the owner. Other Authorization headers are ignored, as before.
- **Draft target vocabulary:** `pending` is kept as the stored value of spec `draft`. The spec 03 states are declared but cannot be entered yet.
- **Comment labels:** any label may follow any other, because labels are the owner's choice.
- **Reply actions:** `uncertain` may only move to `completed` or `failed` (by reconciliation).
- **Job states:** `pending`, `running`, `completed`, `failed`, `reconciling`, `cancelled`. An expired send becomes `reconciling` plus a `reconcile` job.
- **Scheduled sync:** jobs use interval slots and `max_attempts=1`, so a missed slot is covered by the next one. The old in-process scheduler is removed outright.
- **Launcher "stop together":** the launcher never stops healthy services. It rolls back the worker together with the API on a failed launch, and the watchdog run starts the worker again if it is missing.
- **If-Match:** a missing or `*` header gives 428 with code `validation_failed`. A stale version gives 409 `version_conflict`.
- **Idempotency storage:**
  - Only 2xx responses are stored; errors release the key.
  - A crash after the handler committed leaves the key "in progress", so retries get 409 for up to 24 h. That is a refusal rather than a duplicate.
  - The wrapper resolves the principal itself before the route runs.
- **Event stream:**
  - It requires no scope.
  - It starts at the newest event when no Last-Event-ID is given.
  - Ids are global across workspaces. On SQLite, writes are serialized, so ids become visible in order. Postgres will need care: sequence ids can commit out of order.
- **Event payloads:** capped at 4 KB to enforce "ids and small summaries".
- **Retention:** an hourly worker `purge` job deletes idempotency keys older than 24 h and events older than 7 days.
- **Package import:** `runway.api.__init__` now loads `app` and `create_app` lazily. Importing any `runway.api.*` submodule used to build an app, which creates data directories from `.env`.

## Owner questions
1. **Migration rehearsal.** Run a rehearsal on a **copy** of the production database before deploying, after a backup through the SQLite online backup API as in `docs/INTELLIGENCE_OPERATIONS.md`. `0014` rebuilds 15 tables in batch mode, so compare row counts.
2. **Muse and bearer keys.** Does Muse (or anything else) send `Authorization: Bearer rwy_…` today? If so, it now gets 401 until the API-keys job lands.
3. **Stopping Runway by hand.** A manual stop now has to stop the `runway worker` process too. Should `scripts/` gain a stop helper, and should the Supervisor know about the worker?
4. **Event stream scope.** Should the stream require a scope (for example `content:read`), or stay "any principal of the workspace"?

## What the next jobs must know
- **Head and new revisions:** the head is `0016_shared_contracts`; new revisions use `down_revision = "0016_shared_contracts"`. Because `0001` runs `create_all` from the models, every new revision must skip tables and columns that already exist, as `0013`–`0016` do. After a schema change, regenerate the contract with `write_schema_snapshot` and update `tests/integration/test_schema_contract.py`, `test_foundation.py` and `test_canonical_migration.py` (they assert the head).
- **Publishing job:**
  - Add edges to `TARGET_STATE_MACHINE` and keep `TargetState.DRAFT = "pending"`.
  - Pass `JobQueue(database, expired_send_handler=...)` to decide re-run versus reconcile from the attempts ledger.
  - Register handlers in `runway.jobs.runtime.build_worker`.
  - Write `target.state_changed` with `record_event` in the same transaction as the state change.
- **Real-time job:**
  - Write events with `record_event`; types come from `runway.events.types`.
  - Implement `PostgresNotifyWakeup` and select it in `wakeup_for`.
  - Adaptive polling is new producers in `build_worker`.
- **Agent-interface job:**
  - Build every router with `build_v1_router`.
  - Raise `Problem.of(ProblemCode.…)`.
  - Use `@idempotent` on mutations, `Depends(cursor_params)` with `paginate`, and `Depends(if_match_version)` with `bump_version`.
  - API keys plug into `runway.auth.principal.resolve_principal`, which currently refuses `rwy_` keys.
- **Tests that use `create_app`:** the database must be migrated first (use the `database` fixture or `initialize_database`). The lifespan refuses an unmigrated one.
