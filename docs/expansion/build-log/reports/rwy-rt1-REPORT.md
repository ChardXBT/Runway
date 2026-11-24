Done: RT1 webhook inbox (realtime P4 part 1) implemented, all required tests green, committed in three logical steps on `expansion/k-realtime`.

# RT1: webhook inbox — report

## Commits (branch `expansion/k-realtime`, not pushed)

- `f3916b8` feat(db): 0018 webhook inbox tables: platform_subscriptions and webhook_events
- `4a94cbd` feat(api): signed webhook routes for YouTube WebSub, Instagram and TikTok
- `ee4532a` feat(worker): process_webhook turns verified hints into targeted fetches

## Files changed

- `alembic/versions/0018_webhook_inbox.py` (new)
- `src/runway/db/models.py` — `PlatformSubscription`, `WebhookEvent`
- `docs/schema/intelligence-data-flywheel.json` — regenerated (migration `0018_webhook_inbox`, 76 tables)
- `src/runway/config/settings.py` — `instagram_webhook_verify_token` (secret), `webhook_rate_limit_per_minute=120`
- `src/runway/jobs/queue.py` — `JobQueue.enqueue_pending_job(session, ...)`: enqueue inside the caller's transaction
- `src/runway/realtime/__init__.py`, `hooks_routes.py`, `processing.py`, `hooks.py` (new package)
- `src/runway/api/app.py` — mounts `build_hooks_router`
- `src/runway/jobs/runtime.py` — registers `process_webhook` in `build_worker`
- `src/runway/jobs/retention.py` — purges `webhook_events` older than 30 days
- `tests/integration/test_webhook_inbox.py` (new, 12 tests), `test_webhook_inbox_migration.py` (new, 7 tests)
- `tests/integration/test_route_principal.py` — `EXEMPT_SIGNED_WEBHOOKS`
- `tests/integration/test_schema_contract.py` — `platform_subscriptions` added to `WORKSPACE_SCOPED_TABLES` (`webhook_events.workspace_id` is nullable by design, so it is not in that list)
- `tests/unit/test_job_queue.py` — handler set now includes `process_webhook`

## Migrations

- `0018_webhook_inbox`, `down_revision = "0016_shared_contracts"` (head was 0016; no 0017 exists). Per-table and per-index existence checks, offline-mode guard, Postgres-safe, crash-safe (verified by tests: mid-revision crash rolls back; a partial run completes).

## Tests (all via the testlock wrapper)

Fail-before evidence, as required:

- `.venv/Scripts/python.exe .../testlock.py .venv/Scripts/python.exe -m pytest tests/integration/test_webhook_inbox_migration.py -q -p no:cacheprovider` → **6 failed, 1 passed** (missing `webhook_events`/`platform_subscriptions` tables, missing SQL, `no such table`).
- Same wrapper, `tests/integration/test_webhook_inbox.py` → **collection error, 0 ran**: `ModuleNotFoundError: No module named 'runway.realtime'` (all 12 acceptance tests therefore fail before implementation).

After implementation:

- `pytest tests/integration/test_webhook_inbox.py tests/integration/test_webhook_inbox_migration.py tests/integration/test_route_principal.py tests/integration/test_schema_contract.py tests/integration/test_workspace_migrations.py tests/integration/test_database_lifecycle.py tests/integration/test_event_stream_http.py tests/unit/test_job_queue.py tests/unit/test_worker_startup.py -q -p no:cacheprovider` → **93 passed** in 210 s.
- `pytest tests/unit -x -q -p no:cacheprovider` → **859 passed, 1 skipped** in 263 s.
- `ruff check .` → All checks passed. `ruff format --check .` → 292 files already formatted.
- `mypy src/runway` (wrapper) → Success: no issues found in 464 source files.

## Behavior notes

- POST order: body cap (Content-Length fast-path + streaming cap, chunked included) → per-(provider, source) rate limit → raw-body signature verify (`hmac.compare_digest`) → persist (`dedupe_key` only when verified) → `process_webhook` job in the same transaction → respond (200 `{"ok": true}` / 403 / 413 / 429). Verified duplicates return 200 with no second row and no job.
- YouTube WebSub GET: matching topic + `pending|active` state → `active`, stores lease/`expires_at`, echoes challenge; `unsubscribe` with matching topic → `unsubscribed` + echo; anything else 404. POST accepts `sha1=` and `sha256=`.
- Instagram GET: challenge only on constant-time `instagram_webhook_verify_token` match. POST: `X-Hub-Signature-256` over the raw body with `instagram_app_secret`.
- TikTok POST: `t=<unix>,s=<hex>`, HMAC-SHA256 over `t.` + body, ±300 s skew; `authorization.removed` → `needs_reauth` + pending-job cancellation + `account.updated`; `post.publish.*` → `target.progress` on the account (`hooks.on_publish_event`); others → `sync_account`.
- YouTube/Instagram/other TikTok hints enqueue `sync_account` with the scheduler payload `{"account_id", "trigger": "webhook"}`, idempotency key `sync_account:<account>:webhook:<event>`.

## Deviations and decisions

- `PROCESS_WEBHOOK_JOB` lives in `runway/realtime/__init__.py` (re-exported from `processing.py`) so the routes commit does not depend on the processing commit.
- `enqueue_pending_job` was added to `JobQueue` rather than enqueuing rows directly, keeping the "same transaction" rule literal.
- `webhook_events.workspace_id` is a nullable FK to `workspaces` (set on resolution); it is deliberately not in `WORKSPACE_SCOPED_TABLES`.
- LocalRequestGuard needs no change: loopback `TestClient` requests pass; the public-edge allow-list is the next job (per brief).
- `hub.mode` other than `subscribe`/`unsubscribe` and missing `hub.challenge`/`hub.topic` → 404 on WebSub GET.
- Unverified events can never carry a `process_webhook` job; the handler additionally drops any such job with `error="unverified signature"` as defence-in-depth.
- The shared testlock made the fail-before run wait ~43 min behind other builders; no processes were disturbed.

## Owner questions

None.

## Next builder (RT2+)

- `build_hooks_router(database, settings)` is mounted in `create_app`; `PROCESS_WEBHOOK_JOB` is registered in `build_worker`.
- `JobQueue.enqueue_pending_job(session, kind, *, idempotency_key, ...)` enqueues inside an existing transaction.
- `PlatformSubscription` is ready for a WebSub renew/resubscribe job (`expires_at`, `state`, `hub_secret`).
- The public-edge path for webhooks (accepting non-loopback Host on `/hooks/*` only) is intentionally not built yet.
- `hooks.on_publish_event` is the seam where publish-target lookup lands when the publishing engine merges.
