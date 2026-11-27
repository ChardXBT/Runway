# Verification report — creator platform expansion (2026-10-17)

This report records what was actually run to verify the creator-platform expansion, with
results. It distinguishes offline verification (tests, fixtures, mocked transports) from live
verification (a real authorized account). **No live platform account was connected during this
work**, so every platform integration below is verified offline only.

## Environment

- Windows 11, Python 3.13.2 (`.venv`), Node 24.14.0 / npm 11.9.0.
- Tests run against temporary data directories (`tests/conftest.py` forces this). The production
  data directory was never used by tests or by the browser checks.

## Automated checks

| Check | Command | Result |
| --- | --- | --- |
| Lint | `python -m ruff check .` | pass |
| Format | `python -m ruff format --check .` | 210 files already formatted |
| Types | `python -m mypy src/runway` (strict) | no issues in 128 source files |
| Backend tests | `python -m pytest -p no:cacheprovider -q` (full suite) | 434 tests collected; 430 passed on the first full run, 4 failed on assertions that our own changes made stale (alembic head is now `0012_creator_platforms`; provider handles are stored without a leading `@`). The four assertions were corrected and re-run: 4 passed. Runtime 41 min 52 s. |
| Windows launcher contract | `python -m pytest tests/unit/test_windows_launcher.py` | 3 passed |
| Dependency audit (Python) | `python -m pip_audit` | no known vulnerabilities |
| Frontend | `npm run check` (ESLint, `tsc --noEmit`, Vitest, `next build`) | pass; Vitest 27 files / 154 tests |
| Dependency audit (npm) | `npm audit`; `npm --prefix apps/web audit --omit=dev`; `node scripts/audit_web_dependencies.mjs` | 0 vulnerabilities; 0 vulnerabilities; clean with no recorded exceptions (the reviewed dev-only `braces` exception was retired after a patched release resolved the advisory) |
| Existing fixture workflow | `python scripts/e2e_demo.py` | exit 0, proof report written under `data/proofs/` |

New backend tests (offline, `httpx.MockTransport` for live providers): capability matrix and
readiness, token store (Fernet round trip, tampering, missing key), OAuth state (single use,
expiry, platform mismatch, PKCE), YouTube / Instagram / TikTok providers (identity, pagination,
batching, duration parsing, analytics rows, comment threads, error mapping, scope fallbacks,
handle normalization), sync (idempotent re-import, cursor head-refresh + backfill, partial runs,
`needs_reauth`, rate limits, concurrency, stale-run cleanup, bounds, draft-target auto-link),
inbox (unanswered predicate, statuses, drafts, reply ledger and interlock), drafts (URL parsing
for mark-published), analytics aggregation, overview, settings secrecy, platform routes
(TestClient; OAuth callback outcomes; request guard still enforced; `/health` unchanged), and
the 0011 → 0012 → 0011 migration with data preserved.

New frontend tests: nav groups/active state/drawer focus handling, demo banner, state panel,
metric tile, format helpers (`parseApiDate`, `formatHandle`), trend chart, accounts manager
(connect, sync polling, two-step disconnect, error codes), dashboard, analytics workspace, inbox
workspace (default filters, draft autosave, status actions, reply gating, uncertain result),
content library and detail, drafts workspace.

## Database migration rehearsal (production copy)

The production database (`data/qlob-production/runway.db`, 467,283,968 bytes) was copied with
SQLite's online backup API to a temporary directory. On the copy: `PRAGMA integrity_check` = ok;
row counts were identical before the rehearsal, after `alembic upgrade head`, and after
`alembic downgrade 0011_runtime_performance`:

```
channels 1 · posts 771 · media_assets 1545 · proposals 81 · proposal_events 437 ·
caption_feedback 94 · publish_attempts 30 · audit_events 3383 · feedback_signals 282 ·
pairwise_preferences 396 · representation_records 4816
```

All 14 new tables were present at head and absent after the downgrade. The original file's size
and mtime were unchanged by the rehearsal.

Disclosure: the production database itself was found already at revision
`0012_creator_platforms` when the rehearsal ran. Runway creates its FastAPI app at import time
and reads `.env`, so a backend process started during development without an explicit
`RUNWAY_DATA_DIR` override applied the additive migration to the production file (mtime
2026-10-17 20:21). The migration only adds tables; the new tables contain zero rows (checked
read-only), integrity is ok, and all pre-existing counts match the values above. This is the
same migration the next launch would have applied.

## Browser checks

Playwright against a running API + Next.js dev server on temporary data directories.

- **Mock providers** (`RUNWAY_PLATFORM_PROVIDER=mock`): `/dashboard`, `/accounts`, `/analytics`
  (YouTube and TikTok accounts), `/inbox` (list and a selected comment), `/content`,
  `/content/<id>`, `/drafts` (after creating a two-target draft and marking one target
  published), plus the pre-existing `/review`, `/lineup`, `/archive`, `/settings`, `/connector`,
  `/profile`, `/activity`. Captured at 1440×900 and 390×844 (1024 spot checks for the tables).
  Zero console errors or warnings.
- **Live providers without credentials** (fresh data directory): the same 13 routes return 200
  with zero console errors, and the platform pages show their not-configured / empty states.
- The `impeccable detect` design check over `apps/web` reports one finding, a pre-existing 3px
  inset stripe on `.connector-state-checking` in the original connector UI, left unchanged.

## Supervisor compatibility

- `GET /health` returns `status: "ok"` and the configured product name (the operator's `.env`
  sets `RUNWAY_PRODUCT_NAME=RunWay`; Supervisor compares case-insensitively). One new key,
  `platform_provider`, was added; nothing else changed.
- `apps/web/app/health/route.ts`, `run-runway.ps1`, and the `/review` route are unchanged
  (`git diff --quiet` against the previous commit).
- The request guard still answers 421 to non-loopback `Host` headers (observed during the check).

## Security checks

- `git status` shows no tracked change under `data/`; `.env` is untracked; `data/secrets/` is
  explicitly ignored. A grep for token patterns (`gho_`, `ya29.`, `EAAC…`, `act.…`,
  `BEGIN PRIVATE KEY`, `client_secret=`) over tracked and unignored files found nothing.
- `GET /api/settings` exposes only `*_oauth_configured` booleans for platform credentials (unit
  tested against a Settings instance with fake secrets).
- Mock fixtures are fictional ("Northwind Studio") with no media URLs.

## Acceptance run on Windows (2026-11-21)

A fresh checkout of `main` (after the 2026-11-17 readiness audit, PR #4) was installed and run on
the owner's Windows 11 PC: python.org Python 3.12.10 `.venv`, Node 24.16.0 / npm 11.13.0. That
machine had no `.env`, so no platform credentials were available and no real account was
connected. Everything that could run without the owner's accounts was run:

| Check | Result |
| --- | --- |
| `pip install -r requirements.lock`, `npm ci`, `npm run web:build` | clean; production build written |
| `runway init`, `runway doctor` | all `[OK]` (Node 24.16.0, Playwright Chromium, migration `0012_creator_platforms`) |
| `scripts/e2e_demo.py` | exit 0, fixture proof written |
| `run-runway.ps1` | **failed before the fix below**, then API and web ready |
| `ruff check`, `ruff format --check`, `mypy src/runway` | pass; 213 files formatted; no issues in 126 files |
| Windows launcher contracts + platform route/regression suites | 22 passed |
| Mock mode: all 16 pages | 200, no error panels |
| Mock mode: sync, inbox reply (Instagram), disconnect, reconnect | sync 202 then `completed`; reply recorded and verified with no network call; disconnect clears the token; reconnect refused with 400 in mock mode |
| Live mode, no credentials | `/api/platforms` reports `configured: false` with the required variables; YouTube and TikTok connect return 409 |
| Live mode, Instagram token import with a fake token | Meta's real endpoint rejected it (400 `Failed to decrypt`); neither the token nor a `graph.instagram.com` URL appeared in the service logs (audit fix #9 holds against the real endpoint) |

Defects found and fixed in this run:

1. `run-runway.ps1` rejected its own API on python.org Python 3.12. The venv `python.exe` is a
   redirector that starts the base interpreter, so the listener's command line names
   `...\Python312\python.exe`, not the checkout. Every launch ended with "Port 8000 is occupied
   by an unrelated or unverifiable process". The launcher now accepts that listener only when
   its parent is this checkout's venv redirector running `runway.api.app:app`.
2. Syncing a disconnected account (possible from Analytics "Sync now") relabelled it
   `needs_reauth` with "account has no stored token". It now returns 409 asking for a reconnect,
   records no run, and leaves the status alone.

Still outstanding, because it needs the owner's credentials and accounts: connecting,
disconnecting and reconnecting a real YouTube channel; a sync and a reply more than an hour after
connecting (token refresh); importing and syncing a real Instagram token; and a TikTok connection
through an HTTPS tunnel. These are tracked as GitHub issues.

## Verified live capabilities

None. Every YouTube, Instagram, and TikTok capability marked *implemented* in
[`PLATFORM_CAPABILITIES.md`](PLATFORM_CAPABILITIES.md) is implemented against the documented
response shapes and verified with mocked transports only. The first connection of a real account
should be treated as an acceptance test; the Accounts page and sync-run steps are built to make
any mismatch visible rather than silent.

## Blocked by credentials or approval

- YouTube: a Google Cloud OAuth client (`RUNWAY_YOUTUBE_CLIENT_ID/SECRET`) is required to connect.
- Instagram: a Meta app with Instagram Login (`RUNWAY_INSTAGRAM_APP_ID/SECRET`) and a
  professional account; publishing additionally needs a public media host (not provided).
- TikTok: a TikTok for Developers app (`RUNWAY_TIKTOK_CLIENT_KEY/SECRET`), an HTTPS redirect via
  a Host-rewriting tunnel for local use; comments need the separate TikTok for Business API;
  publishing needs a Content Posting API audit.

## Remaining risks

- Live response shapes may differ from documentation in small ways (field renames, additional
  pagination quirks); parsers are defensive but untested against production traffic.
- Reply sending (behind `RUNWAY_INBOX_REPLIES_ENABLED`) has not posted a real reply; its
  verification step is tested only against mocked responses.
- The content sync labels YouTube items ≤180 s as "short"; the API has no Shorts flag.
- Webhooks are not implemented; freshness depends on manual or interval sync.

## Re-verification on the owner's laptop

The configured Runway data directory was located from the live checkout's settings. The
database was opened read-only and copied with SQLite's online backup API before rehearsal.
The source was at `0012_creator_platforms`, with 66 tables, 405,336 rows, an `ok`
integrity check, and zero foreign-key violations.

The release candidate was rehearsed on two disposable copies outside the repository:

- Copy A upgraded to `0022_account_tokens`. Every source table kept its row count. The
  upgraded copy passed the integrity check with zero foreign-key violations.
- Copy B upgraded and downgraded to `0012_creator_platforms`. It returned to the source
  table set and row counts. The downgraded copy passed the integrity check with zero
  foreign-key violations.

The source database was not modified. The recovery backup and scratch copies were kept
outside the public repository. The scratch copies were removed after verification.

The release candidate checks passed locally for Ruff, formatting, strict mypy, the focused
OAuth, quota, request-policy, platform, and migration tests, and the production-copy
rehearsal. GitHub CI and secret scanning passed on commit \`aece546\`, including backend,
Postgres, frontend, Windows launcher, and container-image jobs.
