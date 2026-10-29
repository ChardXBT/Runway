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
