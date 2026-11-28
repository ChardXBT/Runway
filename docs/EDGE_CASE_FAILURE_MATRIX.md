# Runway edge-case failure matrix

Review date: 2026-11-27

Base reviewed: `main` / `9cae17a443ec4126a3f2d877e7dfc1b59e1254cd` (`Port generic blind-export provenance and UTC timestamp fixes to main (#29)`)
Review branch: `devin-free/33-edge-case-review`

Issue: #33, “Edge-case review: Runway failure matrix”

## Verdict

The review deliverable is complete for the requested offline scope. Behavior remediation is owner-decision gated because the correct fix depends on whether main receives small ports or waits for the owner-held expansion architecture. No live account, production database, production setting, deployment, credential, browser session, or external write was used.

The most important demonstrated main-branch gaps are:

1. **Authorized publishing queue is duplicated across processes.** Two independent `YouTubeBrowserPublisher` objects, the same database, and one queued attempt both succeeded and both invoked their non-network scheduling adapters. Evidence: `schedule_calls_per_process_adapter [1, 1]`, two `youtube_submission_started` audit events, outcomes `success` for both workers.
2. **Platform sync run admission is duplicated across processes.** Two independent `SyncService` objects created two simultaneous `running` rows for the same account: `[(1, 1, 'running'), (2, 1, 'running')]`.
3. **A partial pre-existing migration table is silently accepted as complete.** A fixture database with only `CREATE TABLE connected_accounts (id INTEGER PRIMARY KEY)` reached Alembic head `0012_creator_platforms` and `runway init` exited 0, while `runway database schema-verify` exited 1 and a normal ORM account query failed with `sqlite3.OperationalError: no such column: connected_accounts.platform`.

Additional source-verified control gaps, not exploit reproductions:

- disconnect ignores a false revoke result and returns `disconnected` even when provider revocation did not happen or is unsupported (`src/runway/api/platform_routes.py:488-509`); provider contracts return `bool` (`src/runway/platforms/base.py:283-285`) and Instagram explicitly returns `False` (`src/runway/platforms/instagram.py:207-208`). The local token is still deleted, but the caller is not told that provider-side revocation is absent or failed.
- token refresh is protected only during each individual token-file read/write; the read-provider-refresh-write sequence in `ProviderRegistry.fresh_token` is not serialized across processes (`src/runway/platforms/registry.py:79-99`).
- reviewer export writes the final JSON directly with `output.write_text` (`src/runway/intelligence/studies.py:489-491`), so an interrupted write can leave a partial export file even though re-export remains idempotent.

## Scope and overlap

### In scope on main

- OAuth connection/callback/token boundaries: `src/runway/platforms/oauth.py`, `tokens.py`, `registry.py`, `api/platform_routes.py`
- publishing and Lineup: `src/runway/publishing/youtube.py`, `src/runway/domain/state_machine.py`, `tests/integration/test_youtube_publisher.py`
- migration/schema bootstrap: `alembic/versions/0012_creator_platforms.py`, `src/runway/db/bootstrap.py`, schema verification commands
- reviewer exports and active-learning state: `src/runway/intelligence/studies.py`, `tests/integration/test_study_active_learning.py`
- scheduled work on main: `src/runway/platforms/sync.py`, `src/runway/platforms/drafts.py`, `apps/web/lib/datetime.ts`

### Existing overlapping work

- PR #16 (`expansion/build`) is owner-held and unmerged. It contains workspaces, durable `publish_jobs`, worker leases, adaptive sync, hosted auth/runtime, real-time events, and extensive unit/integration coverage not present on main. Matrix rows in the “Workspace jobs” column are explicitly marked `EXPANSION` because that behavior cannot be executed from the main working tree without adopting another feature branch.
- PR #20 is owner-held and unmerged. It covers migration rehearsal/backup/restore evidence and should not be duplicated here.
- PR #23 was superseded by PR #29 on main; the generic blind-export provenance guard and UTC timestamp fixes are present in the reviewed base.
- PR #28 is owner-held and unmerged. It covers production rehearsal and workspace lifecycle guards.

This review therefore uses main as the executable base and inspects expansion only for overlap/narrowing. It does not re-review PRs #16, #20, or #28 as substitutes for those implementation reviews.

## Exact evidence commands and counts

Baseline commands run before the report existed:

```powershell
# Python 3.13 venv prepared offline with uv, then:
.venv/bin/python -m pytest
```

Baseline exact result:

```text
535 tests collected
531 passed, 4 skipped, 1 warning in 441.18s (0:07:21)
```

Frontend baseline command:

```bash
npm run check   # from the repository root
```

Baseline exact result:

```text
Test Files  32 passed (32)
Tests       179 passed (179)
```

After-report backend command:

```bash
.venv/bin/python -m pytest
```

After-report backend exact result:

```text
535 tests collected
531 passed, 4 skipped, 1 warning in 438.05s (0:07:18)
```

After-report frontend command:

```bash
npm run check   # from the repository root
```

After-report frontend exact result:

```text
Test Files  32 passed (32)
Tests       179 passed (179)
```

Startup smoke after the report:

```bash
RUNWAY_DATA_DIR=/tmp/runway-smoke/data RUNWAY_AGENT_RUNTIME=mock RUNWAY_PUBLISHING_ENABLED=false .venv/bin/python -m runway.cli.main init
RUNWAY_DATA_DIR=/tmp/runway-smoke/data RUNWAY_AGENT_RUNTIME=mock RUNWAY_PUBLISHING_ENABLED=false .venv/bin/python -m runway.cli.main doctor
```

Exact startup smoke result: `init` exit 0; `doctor` exit 0 with Python 3.13.18, Node v24.22.2, npm 10.8.3, migration `0012_creator_platforms`, mock runtime, and loopback API port all OK. The only warning was that Playwright Chromium was not installed; Google Chrome publishing path was present. No browser or platform action was launched.

Migration fixture commands (fixture directory `/tmp/runway-edge-migration`, no production data):

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('/tmp/runway-edge-migration/data/runway.db')
con.execute('CREATE TABLE connected_accounts (id INTEGER PRIMARY KEY)')
con.commit(); con.close()
PY
RUNWAY_DATA_DIR=/tmp/runway-edge-migration/data RUNWAY_AGENT_RUNTIME=mock RUNWAY_PUBLISHING_ENABLED=false .venv/bin/python -m runway.cli.main init
RUNWAY_DATA_DIR=/tmp/runway-edge-migration/data RUNWAY_AGENT_RUNTIME=mock RUNWAY_PUBLISHING_ENABLED=false .venv/bin/python -m runway.cli.main database schema-verify
```

Exact fixture results:

```text
init exit code: 0
schema-verify exit code: 1
alembic version: 0012_creator_platforms
connected_accounts columns: [(0, 'id', 'INTEGER', 0, None, 1)]
normal ORM query: OperationalError (sqlite3.OperationalError) no such column: connected_accounts.platform
schema-verify JSON: matches=false; missing connected_accounts columns included account_type, avatar_url, connected_at, created_at, platform, and others
```

Sync fixture used two independent `SyncService` objects over one fixture SQLite database, with a controlled barrier inserted only to reproduce a valid scheduler interleaving after both non-blocking recent-run checks returned false. Exact output:

```text
begin_results [(0, 2), (1, 1)]
errors []
sync_runs [(1, 1, 'running'), (2, 1, 'running')]
```

Publishing fixture used the repository's offline `FakeYouTubeAdapter` and full fixture proposal from `tests/integration/test_youtube_publisher.py`. A controlled barrier reproduced the valid cross-worker interleaving after both workers read the queued row. Exact output:

```text
attempt_id 1
outcomes [(0, 'success', 'externally_scheduled'), (1, 'success', 'externally_scheduled')]
schedule_calls_per_process_adapter [1, 1]
final_attempt (1, 'verified', 'Ugfixture')
final_proposal (1, 'externally_scheduled', 'Ugfixture')
submission_started_events 2
```

The adapter was non-network; no YouTube/browser/live account action occurred.

## Failure matrix legend

- **HANDLED** — normal path rejects, redacts, retries, or settles consistently and existing tests/source directly support that classification.
- **PARTIAL** — important protections exist, but a defined residual failure mode remains.
- **GAP-DUP** — duplicate external/state work is possible.
- **GAP-SILENT** — invalid or incomplete state is accepted or not surfaced.
- **SAFE NO-OP** — the input does not start external work and produces no state corruption.
- **EXPANSION** — main does not contain this feature; evidence is in the unmerged expansion branch and is cited for overlap, not executed here.
- **N/A** — input does not apply to a normal entry point.

## Concrete matrix

| Edge input | Workspace jobs | OAuth/token boundary | Publishing/Lineup | Migration/schema | Reviewer exports | Scheduled sync/drafts |
|---|---|---|---|---|---|---|
| empty | EXPANSION: `{}` is a valid full-sync/default payload; non-object/corrupt payload is failed once. | HANDLED: empty/missing `code` or `state` redirects to a short error; empty imported token rejected by length bounds. | HANDLED: empty caption/image rejected; explicit empty Lineup selection is never expanded to all posts. | GAP-SILENT for an empty/incomplete pre-existing platform table; clean empty database migrates correctly. | HANDLED: empty reviewer metadata is scrubbed to `{}`/omitted; empty/blank response names rejected. | HANDLED: empty provider pages cannot wedge the resume cursor; no content/comments are a safe no-op. |
| missing/null | EXPANSION: missing scope means full sync by design; malformed/non-object payload fails once; missing/expired lease is recovered through expiry/reconciliation. | HANDLED: missing token means `needs_reauth`; null token expiry does not force refresh. | HANDLED: missing local image invalidates preflight; missing attempt raises before adapter work. | PARTIAL: missing database is created; missing columns inside a pre-existing table hit the demonstrated gap. | PARTIAL: ordinary missing media is prevented by FK/schema, but a corrupt missing media row is not explicitly reported before export lookup. | HANDLED: missing account raises `SyncAccountMissing`; missing cursor restarts bounded backfill instead of treating state as complete. |
| zero/negative | EXPANSION: invalid `max_attempts` rejected; failure counters clamp at zero. | HANDLED: zero/negative remaining token lifetime is treated as expired/refresh-required; response/token validation bounds lengths. | HANDLED: zero-byte image rejected; exact five-minute lead boundary and publication timing are tested. | HANDLED for data volume: zero-row pre-existing tables remain user data where migration guards skip creation. | HANDLED: zero/negative fixture metadata values serialize literally because they are data, not provenance keys. | HANDLED: zero sync interval disables the fixed scheduler; zero comments budget avoids comment sync. |
| huge | EXPANSION: retry delay is bounded and errors recorded rather than unbounded worker loops. | PARTIAL: oversized state does not match any row; import token length is capped, but provider strings are not uniformly schema-capped. | HANDLED: 16 MiB image cap, caption length cap, batch deduplication, and strict payload validation. | PARTIAL: large row counts are only a performance/rehearsal concern; no main fixture proves very large migrations. | HANDLED: large metadata serializes deterministically; NaN/Infinity are rejected by `allow_nan=False`. | PARTIAL: provider counters are converted to SQLite integers; out-of-range values would fail the metrics step rather than be clamped, which is fail-closed but crude. |
| Unicode | EXPANSION: JSON payloads are Unicode-capable; no special gap found in inspected tests. | HANDLED: OAuth account labels and redirect fragments avoid secret-bearing detail; token JSON is UTF-8. | HANDLED: captions preserve Unicode and use explicit length validation. | HANDLED: SQLite TEXT stores Unicode; migration does not transform text payloads. | HANDLED: exports use UTF-8 and `ensure_ascii=False`; Unicode fixture scene is tested. | HANDLED: captions/comments/content text pass through as Unicode strings. |
| duplicate | EXPANSION: enqueue is idempotent on key; concurrent enqueues create one job. | HANDLED: token store serializes file mutation; OAuth state is single-use by conditional update. | GAP-DUP: cross-process queue workers duplicated one scheduling action; prepare re-validation also prevents duplicates in-process. | HANDLED for repeated upgrade: guards avoid duplicate table creation, but this same mechanism causes the partial-table gap. | HANDLED: repeat plan returns the existing study; repeat export deterministically overwrites the same JSON instead of duplicating state. | GAP-DUP: cross-process sync admission created two running rows; manual duplicate clicks inside one process are locked. |
| reordered/out-of-order | EXPANSION: jobs claim earliest due `run_at` then id, not insertion order alone. | HANDLED: state consumption does not depend on callback ordering beyond expiry; mismatched platform/state rejected. | HANDLED: batch validation sorts posts by planned publish time; next queued row uses stable id order. | HANDLED: Alembic revision order is linear; schema fingerprint catches drift when explicitly verified. | HANDLED: blind arm order/hidden labels are never exposed in export. | HANDLED: YouTube activities/pages are cursor/published-at bounded; empty out-of-order pages do not pin cursors incorrectly. |
| stale | EXPANSION: expired leases rerun ordinary jobs but send jobs go to reconciliation, never automatic resend. | HANDLED: expired OAuth state cannot be consumed; stale transient refresh does not mark account revoked. | HANDLED: stale prepared/queued attempts become verification-only or reject new submission; corrupt rows become UNKNOWN. | PARTIAL: schema verify detects stale/incomplete schema, but startup does not enforce it. | HANDLED: previously exported study is reused; content changes are versioned/hash-bound rather than silently rewritten. | PARTIAL: stale sync runs are marked failed at startup; long-running process recovery is limited and clock dependent. |
| future-dated | EXPANSION: future `run_at` is not claimed until due. | HANDLED: future expiry extends state validity only within ten-minute issuance; attacker cannot mint a state. | HANDLED: future schedule times must respect configured minimum lead; explicit schedule time is rendered in channel timezone. | N/A: migrations do not use business dates. | HANDLED: decision timestamps are UTC-normalized for export. | HANDLED/PARTIAL: future-dated drafts are display metadata only; no main scheduler auto-publishes them. Lineup future times are validated. |
| partial | EXPANSION: partial handler failure records failure/retry; corrupt job does not stop next job. | PARTIAL: OAuth callback consumes state before exchange, avoiding replay but requiring a fresh connect after provider exchange failure. | HANDLED: partial batch preflight fails before queueing; partial adapter submission is verification-only. | GAP-SILENT: partial pre-existing table reaches head but schema mismatch remains undiscovered unless schema-verify is run manually. | PARTIAL: direct non-atomic file write can leave partial JSON after interruption. | PARTIAL: content/comment/metric step failures are isolated per account step, but a cross-process duplicate run can double work. |
| timeout | EXPANSION: lease expiry plus heartbeat separates crash from slow work; failing idle query backs off. | HANDLED: provider refresh timeout is transient and does not mark `needs_reauth`. | HANDLED: adapter/browser timeouts fail closed and preserve verification evidence rather than retrying ambiguous submissions. | N/A: migration has no timeout contract; interruption is represented by partial/crash rows. | N/A: local file serialization has no network timeout. | HANDLED: sync scheduler catches exceptions and keeps ticking; individual provider timeouts mark run failure. |
| retry | EXPANSION: transient failures retry with exponential bounded backoff; permanent failures do not. | HANDLED: transient refresh failures do not revoke account; explicit reconnect creates a new state. | HANDLED: ambiguous submissions are never automatically retried; superseded attempts are not reused. | PARTIAL: migration re-run is table-idempotent but not schema-correctness idempotent for partial tables. | HANDLED: repeated export is idempotent and overwritten only after deterministic serialization. | HANDLED/PARTIAL: sync can be manually retried; stale failed runs are auditable, but duplicate concurrent retries remain possible across processes. |
| concurrent | EXPANSION: SQLite `BEGIN IMMEDIATE` and Postgres `FOR UPDATE SKIP LOCKED`; lease-holder completion enforced. | PARTIAL: OAuth consume is atomic and token file writes are locked, but refresh read-call-write is not serialized across processes. | GAP-DUP: `_confirm_lock` is process-local; no database lease/conditional claim around `QUEUED` rows. | PARTIAL: SQLite DDL can interleave/crash; no startup schema gate. | HANDLED: deterministic export target overwrite is idempotent; concurrent writers can still race last-write-wins but content is deterministic for same study version. | GAP-DUP: process-local account locks do not exclude another process; no active-run uniqueness constraint. |
| crash-after-write | EXPANSION: expired send never auto-reruns; reconcile path preserves ambiguous external state. | PARTIAL: DB connected account write precedes token-store write; crash between them can leave an account row without a usable token, then normal freshness flow reports missing/reauth rather than silent success. | HANDLED/PARTIAL: submission evidence is written before confirmation verification; ambiguous states block blind retry, but duplicate pre-submission race remains. | GAP-SILENT: crash/partial DDL plus table guards can stamp head on incomplete schema. | PARTIAL: direct write can leave truncated JSON; database status update after write means crash causes safe re-export, not a false exported state. | HANDLED: sync rows left running are swept failed at startup; no automatic state destruction. |
| clock-skew | EXPANSION: leases use injected clock and tests cover expiry; real machines still depend on system clock. | HANDLED/PARTIAL: OAuth expiry is server-local UTC; skew can shorten/lengthen the ten-minute window but state remains single-use/random. | HANDLED/PARTIAL: schedule lead checks use local UTC; backward skew can accept too-soon posts, while forward skew makes validation stricter. | N/A. | HANDLED: export timestamps normalized to UTC; ordering remains DB-owned. | PARTIAL: stale-run detection and fixed interval use host time; forward skew can age out active runs early. |
| timezone | N/A/EXPANSION: job times are UTC instants. | HANDLED: OAuth expiry uses UTC. | HANDLED: UTC normalization, DST gap, explicit offsets, fractional seconds, naive timestamps, and channel timezone conversion are tested in backend/frontend. | N/A. | HANDLED: main includes the UTC timestamp fixes from PR #29. | HANDLED: drafts store UTC instants; browser-local rendering is explicit and test-covered. |
| rate-limit | EXPANSION: adaptive backoff honors retry-after and caps at one day in inspected tests. | HANDLED: provider 401/403 semantics distinguish revoked authorization from transient errors; rate limits are provider errors unless explicit retry metadata exists. | HANDLED: adapter/publishing failures are explicit and no blind retry occurs. | N/A. | N/A. | HANDLED/PARTIAL: failed sync step records error and leaves account connected; no adaptive backoff exists on main fixed scheduler. |
| provider-error | EXPANSION: handler error records failure; worker survives failing jobs/queries. | HANDLED/PARTIAL: callback redirects generic errors without detail leakage; refresh provider errors are transient. Disconnect revoke false/failure is not surfaced. | HANDLED: adapter exception records failed attempt/error and does not fabricate success. | N/A. | N/A: local export. | HANDLED: provider exceptions fail the run/step, record `last_error`, and scheduler continues. |

## Skeptical pass

A separate skeptical pass re-read the three positive findings against implementation and test intent rather than accepting first-pass reproduction output.

1. **Publishing race is real, not merely two adapters succeeding because they are fake.** Both workers passed the same queued-state precondition, recorded two `youtube_submission_started` events, and only then reached their adapters. The production-locking primitive is `threading.Lock` on each publisher instance (`src/runway/publishing/youtube.py:1113`), while the queue selection is a plain ordered select (`src/runway/publishing/youtube.py:1569-1581`) and processing starts with a process-local acquire (`src/runway/publishing/youtube.py:1700-1704`). Existing tests prove serialization only inside one process/coordinator, not across independent Runway processes.
2. **Sync race is not an artifact of calling a private method.** `_begin_run` is the admission step used by the normal `run()` entry point. The injected barrier only forces an interleaving the check-then-insert sequence already permits; it does not bypass the lock or mutate rows directly. There is no database partial unique index or conditional insert representing “one running run per account.”
3. **Migration fixture is a realistic partial-state shape, not an invalid database.** Alembic itself is designed to resume/inspect existing databases, and migration `0012` explicitly tolerates pre-existing tables. The fixture demonstrates the negative side of that tolerance: the guard checks only table existence (`alembic/versions/0012_creator_platforms.py:24-80`), while bootstrap invokes migrations but not schema verification (`src/runway/db/bootstrap.py`). The database reaches head and the application starts far enough to fail later at ordinary account access.
4. **Existing expansion tests do not erase main risk.** Durable-job leases in `expansion/build` are stronger, but they are not merged. Until the owner decides whether to land expansion or port a smaller claim mechanism, main remains exposed.
5. **Not every oddity is a defect.** Fixed sync tick order, sequential account processing, draft dates being informational, and Instagram remote revocation being unsupported are documented or capability-shaped behavior. They are classified as partial/operator-visibility concerns only where the user cannot distinguish local deletion from provider-side revocation.

## Safe follow-ups suitable for a future agent

These are agent-doable because they require no credentials or live systems:

1. Add a database-backed atomic claim/lease for main-branch queued publish attempts, with fixture tests for two independent publisher objects and restart/ambiguous-outcome preservation.
2. Add a database-backed active-sync exclusion (conditional insert or partial unique active-run representation where SQLite semantics allow), with fixture tests for two independent `SyncService` instances.
3. Run schema verification during startup/bootstrap or make migration `0012` validate expected columns before skipping an existing table; add partial-table migration fixtures.
4. Surface provider-revocation outcome distinctly in disconnect responses/UI (local disconnected vs provider revoked vs revocation unsupported/failed), without changing account deletion semantics.
5. Serialize token refresh per token reference across processes, or document and enforce single-writer ownership for token refresh.
6. Write reviewer exports through a temporary file plus atomic rename and add an interrupted-write fixture.
7. Add an explicit unknown-job-kind/dead-letter operator surface in expansion before merge, or document that unknown kinds are intentionally ignored for rolling deploy compatibility.

## Follow-up issues opened

- #35 — Add atomic cross-process claim for queued YouTube publish attempts
- #36 — Prevent duplicate cross-process platform sync runs
- #37 — Fail fast on partial pre-existing creator-platform tables
- #38 — Report provider revocation outcome when disconnecting platform accounts
- #39 — Serialize platform token refresh across processes
- #40 — Write reviewer exports atomically

## Unresolved owner decisions

1. Should main receive small ports of atomic publish/sync admission now, or should those fixes wait for the owner-held expansion/job-worker architecture?
2. Should migration bootstrap fail closed on schema-contract mismatch, or continue starting so local users can inspect/repair data?
3. Is it acceptable for disconnect to mean only “local token deleted” when a provider cannot revoke remotely, or should Runway block/report provider-revocation failure differently?
4. Does the owner want unknown durable-job kinds to dead-letter loudly, or remain pending/ignored for rolling deploy compatibility?

## Non-goals and safety statement

No production database, live OAuth consent, browser profile, live platform, deployment, price/cap/threshold, secret file, external message, merge, or main-branch push was touched. Fixture publishers were offline adapters. Security-control observations above describe missing controls and mitigations from source only; no exploit or live vulnerability reproduction was performed.
