Job name: **rwy-engine**

Job rwy-engine: build phase P2 (publishing engine backend) of the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-engine`, branch
`expansion/k-engine` (based on `expansion/build`, which already contains phase P0, the shared v1
contracts, the `runway.mediastore` package and the private-instance work). It has its own `.venv`.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read the reports of the jobs you build on:
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-p0\REPORT.md` (job queue, worker, principal,
state machines, migrations, shared v1 contracts) and
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-media\REPORT.md` (the `runway.mediastore`
public API). Then read, in your worktree: `docs\expansion\specs\00-principles.md`,
`docs\expansion\specs\02-media-pipeline.md`, `docs\expansion\specs\03-publishing-engine.md` (all),
`docs\expansion\specs\06-agent-interface.md` §4-§6, `docs\expansion\specs\08-platform-adapters.md`
§5-§6, and `docs\expansion\research\recheck-20261122.md`.

Two other jobs run at the same time: `rwy-realtime` (adaptive polling, webhook inbox, WebSub, public
edge; it changes `platforms\sync.py`, `api\security.py` and adds `/hooks/*`) and `rwy-agent` (API
keys, scopes, approval queue backend, MCP server). Do not change those areas beyond a one-line
hook. Real platform publishers (YouTube, Instagram, TikTok) come in the next wave; you build the
port, the assisted adapter, mock publishers and the engine they plug into.

## Work packages (all of them)

**A. Media wiring (P2.1).** Alembic revision `0017_publishing_engine` (down_revision = the head
you find on your branch; say it in the report). Tables `media_assets_v2`, `media_uploads`,
`draft_media(draft_id, asset_id, position)` exactly as spec 02 §3 / spec 03 §1, each with
`workspace_id` default 1. v1 routes (spec 02 §4): `POST /api/v1/media`, `PUT` chunk upload for
`mode: api` (LocalDiskStore), `GET /api/v1/media/{id}/upload` (what has arrived, for resume),
`POST /api/v1/media/{id}/complete` (size + SHA-256 check, `processing`, enqueue `probe_media`),
`GET /api/v1/media/{id}`, `DELETE`. `mode: direct` with `R2Store` when R2 settings are configured
(settings only; no real credentials anywhere). A `probe_media` job handler using the mediastore
probe; `ready` or `rejected` with reason. Previews: in `local` serve the poster through a
guarded route; never serve uploads from a public static mount.

**B. Posts and targets (P2.2).** Extend `content_drafts` and `draft_targets` with the columns in
spec 03 §1 (`source`, `source_ref`, `ai_generated`, `version`, `created_by` on posts; `post_type`,
`planned_at`, `release_state`, `released_by`, `released_at`, `payload_hash`, `state`,
`state_reason`, `external_id`, `version`, `publisher` on targets) in the same revision, with
backfill for existing rows. Extend the draft-target state machine to the full spec 03 §2
vocabulary and rules (edit after release returns to `ready` and clears release; release binds to
`payload_hash` = SHA-256 of canonical JSON of caption, settings, media hashes, account external id
and `planned_at`; the post status is derived from targets by one function, never written).
Typed per-`post_type` settings schemas (spec 08 §2-§4 field tables, including
`has_paid_product_placement` and `made_for_kids` required with no default for YouTube, explicit
privacy required everywhere it exists, the TikTok mandatory-screen fields, Instagram field rules
from the re-check). A target with no explicit privacy cannot be released (invariant 13).

**C. Ledger, exactly-once and worker handlers (P2.3).** Table `target_attempts` (append-only) as
spec 03 §1. Implement spec 03 §3-§4 completely: one job per target per release
(`idempotency_key = target_id:payload_hash`), write intent and commit before any platform call,
`request_sent_at` point-of-no-return marking through a helper adapters must call, failure
classification with `request_may_have_been_delivered`, `uncertain` never auto-retried, reconcile
jobs, bounded retries (3 tries, 1/5/20 min, never past `planned_at + 6 h`), expired-lease handling
for `send` jobs (hand to reconcile if the marker was reached). Job kinds `send`,
`poll_processing`, `verify`, `reconcile` wired into the P0 worker. Scheduling rules from spec 03 §6
(UTC storage, YouTube `native`/`held` lead-time validation, Instagram container pre-creation hook
up to 30 minutes early, missed-window rule: < 60 min late send, otherwise back to `ready` with
reason "missed while offline", rate guards as functions the adapters supply). A global
`publishing_paused` setting checked by the worker before every send.

**D. Publisher port, assisted and mock adapters (P2.4).** The `Publisher` Protocol and
`PublishContext`, `SendResult`, `PollResult`, `VerifyResult`, `ReconcileResult`, `Issue` types
exactly as spec 03 §5. Adapter registry keyed by name; per-account, per-post-type publisher
setting with the default rule from spec 03 §5. `assisted` adapter (checklist, waits for
"mark as published", always available). Mock publishers for every post type that walk the full
machine on a timer, with switches forcing `uncertain`, container `ERROR`, quota exceeded, revoked
token, processing failure (spec 08 §6), active only with `RUNWAY_PLATFORM_PROVIDER=mock`.
Per-account capability function (spec 08 §5) returning `ready | ready_draft_only | needs_scope |
needs_media_host | needs_app_approval | assisted_only` from platform, account scopes, operator app
status settings (`youtube_app_status`, `instagram_app_status`, `tiktok_app_status`,
`youtube_posts_api_access`, all defaulting to the owner-install values) and profile; regenerate
`docs\PLATFORM_CAPABILITIES.md` from it with a script and a test that fails on drift.

**E. API (spec 03 §8) on the shared v1 contracts.** Every route in the spec 03 §8 table under
`/api/v1/posts…` with its scope, `If-Match` version checks (409 `version_conflict`),
`Idempotency-Key` on mutations, problem+json errors, cursor pagination, release requiring the
echoed `payload_hash` and `confirmed: true`, TikTok targets refusing release from any API key or
batch (403 `human_required`). Events written in the same transaction for every target state change
and progress (`target.state_changed`, `target.progress` at most once per second,
`target.awaiting_release` when an agent-created target becomes `ready`, `media.ready`). Keep the
existing `/api/drafts…` routes working as aliases. The "Also post to…" bridge (spec 03 §7) as
`POST /api/v1/proposals/{id}/cross-post` creating a post with `source='proposal'` from an accepted
proposal; nothing created automatically; the proposal's Community-post path untouched.

## Acceptance tests that must exist (spec 03 §10, spec 02 §8)

- Kill-at-every-step: against a fake adapter that records calls, simulate a crash before intent,
  after intent, mid-upload, after the point of no return and after the response; restart the
  worker each time; never a duplicate platform call that creates a post; always a correct final
  state.
- Read timeout on the final call gives `uncertain`, no automatic retry.
- Editing a released target invalidates the release; worker refuses a payload-hash mismatch.
- A target without explicit privacy cannot be released.
- With no credentials at all, the assisted adapter completes a post end to end through the API.
- A chunked upload survives a forced disconnect and resumes without re-sending completed chunks; a
  file with a wrong extension is typed by content; a Reel-invalid file shows an Instagram error and
  still schedules for YouTube; workspace 2 cannot fetch workspace 1's asset by id.
- Idempotent `POST /api/v1/posts` replay creates one post; stale `If-Match` returns 409 and changes
  nothing.

## Done when

All packages built with tests; ruff, ruff format, mypy clean; the test scope in COMMON-RULES rule 7 passes; migration upgrades and downgrades cleanly from the previous head on a temp database with
fixture rows (row counts unchanged). Write in the report the exact Publisher port and context
types, the adapter registration call, the job kinds, the event types and the v1 routes, so the
next wave (real YouTube/Instagram/TikTok adapters and the web UI) can build on them.

Commit in logical steps on `expansion/k-engine`. Report at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-engine\REPORT.md`.
