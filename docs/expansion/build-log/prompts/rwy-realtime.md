Job name: **rwy-realtime**

ORCHESTRATOR NOTE (overrides the base description below): your worktree is at commit 404529e =
phase P0 plus the shared v1 contracts (events table, `record_event`, `/api/v1/events/stream`). It
does NOT yet contain the private-instance work (`instance_is_active`, P6) or the media core. P6 is on
branch `expansion/k-p6` and is receiving review fixes now (read
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\prompts\rwy-p6-fix1.md`, item 6: it exposes
`instance_is_active`, `ensure_active_or_raise()` and a `StandbyError`). Build everything else first.
When `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-p6\REPORT.md` has a first line starting
`Done:` and a "Review fixes" section, run
`git merge --no-ff expansion/k-p6 -m "Merge private instance data into realtime"` in your worktree
and wire the standby rule (4.4) to it. If it is not ready when the rest is done, wait up to 60
minutes (check every 10), then merge it as it is and note it. Your migration is
`0018_webhook_inbox` with down_revision `0016_shared_contracts`. P0 is concurrently getting review
fixes on its own branch (crash-safe migrations, launcher, worker-loop robustness, a `purge` job
kind, a cross-process per-account sync lock); do not duplicate those - register your new job kinds
and keep worker edits minimal.

Job rwy-realtime: build phase P4 (real-time sync, except the SSE stream which already exists) of
the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-realtime`,
branch `expansion/k-realtime` (based on `expansion/build`, which already contains phase P0, the
shared v1 contracts including the `events` table, `record_event` and `/api/v1/events/stream`, the
`runway.mediastore` package and the private-instance work). It has its own `.venv`.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-p0\REPORT.md` (job
queue, worker, principal, token refresh, contracts) and, in your worktree,
`docs\expansion\specs\00-principles.md`, `docs\expansion\specs\04-realtime-sync.md` (all),
`docs\expansion\specs\05-hosted-platform.md` §5 (deletion duties), `docs\expansion\specs\07-private-instance-data.md`
§4 (standby rule), `docs\expansion\research\recheck-20261122.md`, and the YouTube, Instagram and
TikTok research files' real-time/webhook sections.

Two other jobs run at the same time: `rwy-engine` (posts, targets, media tables, publisher port,
`/api/v1/posts`, `/api/v1/media`) and `rwy-agent` (API keys, scopes, approval queue backend, MCP
server). Do not change those areas. When a webhook concerns a publish (TikTok `post.publish.*`),
call a small hook function you define (for example `runway.realtime.hooks.on_publish_event`) that
enqueues a `poll_processing` job for the target found by `publish_id` if such a target lookup
exists, and otherwise only records the event; the engine will connect to it.

## Work packages (all of them)

**4.2 Adaptive polling and quota budget.** Replace the fixed sync interval with per-account,
per-scope `sync_account` jobs (scopes and normal/boosted intervals from spec 04 §3 table),
produced by the scheduler on the P0 job queue. Boost after a publish or push hint (a function
`boost(account_id, scope, minutes)` other code can call). YouTube quota budget per Google project
per Pacific day (10,000 units; separate 100/day upload bucket), with a reserved share for
publishing calls, divided across active accounts, degrading intervals instead of failing; with the
budget at zero, publishing calls still pass and syncs defer with a visible reason. Instagram usage
headers: back off at 80%. Error backoff with jitter; `RateLimited` pushes the next run. Accounts
with no UI activity for 14 days drop to daily. Expose per-account freshness and schedule through
`GET /api/v1/accounts/{id}/sync-status` (next run per scope, last run, `push` or `poll`, today's
quota use, push-enabled flag) and emit `sync.started`, `sync.finished`, `content.imported`,
`comment.new`, `quota.low` events with `record_event`.

**4.3 Webhook inbox.** Alembic revision `0018_webhook_inbox` with `down_revision` set to the head on
your branch (the orchestrator re-chains it after the engine's `0017`; write the down_revision in
the report). Tables `webhook_events` (spec 04 §2) and `platform_subscriptions` (provider, account,
topic, secret reference, lease granted, expires_at, state). Routes `GET/POST
/hooks/youtube/websub/{subscription_id}`, `GET/POST /hooks/instagram`, `POST /hooks/tiktok` with
exactly the verification rules of spec 04 §2 and the re-check (WebSub `hub.challenge`, HMAC-SHA1
`X-Hub-Signature: sha1=` and accept `sha256=`; Instagram `hub.verify_token` and
`X-Hub-Signature-256`; TikTok `TikTok-Signature: t=…,s=…` over `t + "." + raw_body`, 5-minute
tolerance), constant-time compare on raw bytes, persist first, return 200 immediately, 1 MB body
cap, per-source rate limit, dedupe key unique, raw events kept 30 days (purge job). A
`process_webhook` job treats every event as a hint and does a targeted API fetch through the
existing providers; never writes platform data from a body alone. Webhook routes get no session and
no workspace; the workspace comes from the account the event names; unknown accounts are stored and
ignored. Add them to the route-walk exemption list with a reason (signature-authenticated).
YouTube WebSub: subscribe on connect with a per-subscription secret, `renew_subscription` jobs at
80% of the granted lease. Instagram: subscription only when `instagram_app_status=live_advanced`.
TikTok: `authorization.removed` marks the account `needs_reauth`, cancels its pending jobs and
starts the deletion clock; `post.publish.*` calls the hook above.

**Public edge (spec 04 §6).** `RUNWAY_PUBLIC_EDGE_URL` setting. Extend the request guard with an
allow-list: only `/hooks/*` and the OAuth callback paths are accepted from the edge host; every
other path stays loopback-only. Tests prove a session-bearing route is refused through the edge
Host and a hook route is accepted.

**4.4 Data duties.** A daily `purge` job: delete YouTube API data older than 30 days that has not
been refreshed (YouTube policy), raw webhook events older than 30 days, and data of accounts
revoked more than 7 days ago. Instagram `refresh_token` job at about day 50 of a 60-day token,
never before 24 hours. A warning event 14 days before any known refresh-token end. Respect the
private-instance standby rule: a machine that is not the instance lease holder (function from the
P6 work, `instance_is_active`) runs no refresh and no sync jobs.

## Acceptance tests that must exist (spec 04 §7)

Forged webhook stored with `signature_ok=false` and triggers nothing; ten replays of one valid
webhook cause one fetch; with quota budget zero, publishing calls pass and syncs defer with a reason;
a transient DNS failure during refresh leaves the account connected (P0 test may already cover it -
keep it); WebSub renewal scheduled at 80% of the granted lease; edge allow-list tests above.

## Done when

All packages built with tests; ruff, ruff format, mypy clean; the test scope in COMMON-RULES rule 7 passes; migration upgrades and downgrades cleanly on a temp database. Report at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-realtime\REPORT.md`, listing every new
setting, route, table, job kind and event type.
