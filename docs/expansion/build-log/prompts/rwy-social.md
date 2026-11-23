Job name: **rwy-social**

Job rwy-social: build the real Instagram and TikTok publishers (phase P3.2-P3.5) and
production-ready Instagram and TikTok connection for the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-social`,
branch `expansion/k-social`, based on `expansion/build`, which already contains P0, the shared v1
contracts, the media core (including `R2Store`), the publishing engine (Publisher port, assisted
and mock adapters, worker handlers, `/api/v1/posts`), real-time sync (webhook inbox, TikTok
events hook, public edge) and the agent interface. It has its own `.venv`.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read the reports `rwy-p0\REPORT.md`, `rwy-media\REPORT.md`, `rwy-engine\REPORT.md`
and `rwy-realtime\REPORT.md` in `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\`, then in your
worktree `docs\expansion\specs\00-principles.md`, `docs\expansion\specs\02-media-pipeline.md` §6-§7,
`docs\expansion\specs\03-publishing-engine.md`, `docs\expansion\specs\08-platform-adapters.md` §1,
§3, §4, §5, `docs\expansion\research\instagram.md`, `docs\expansion\research\tiktok.md`,
`docs\expansion\research\recheck-20261122.md` §2-§3, and the existing
`src\runway\platforms\instagram.py` and `src\runway\platforms\tiktok.py` providers.

Another job (`rwy-youtube`) and the web UI job (`rwy-web`) run at the same time. Touch only
Instagram/TikTok files plus one registration line per adapter; the UI job builds screens against
the API you expose, so document every new route and payload in the report.

## Work packages

1. **Connection (spec 08 §1, §3, §4).** Incremental consent through
   `POST /api/v1/accounts/{id}/publishing/enable`: Instagram adds
   `instagram_business_content_publish`; TikTok adds `video.upload` (and `video.publish` only when
   `tiktok_app_status=audited`). Store granted scopes after each exchange/refresh; recompute
   capability; identity check before every write. Instagram long-lived token refresh job at about
   day 50, never before 24 h (if realtime already added it, reuse it). TikTok: lifetimes from each
   response, refresh under the per-account lock, persist the rotated refresh token before release.

2. **`instagram_direct` publisher** for `instagram_reel`, `instagram_image`, `instagram_carousel`,
   `instagram_story`, exactly spec 08 §3 "Send" including the 2026-11-22 additions: preflight
   (scope, `content_publishing_limit` with 50/24 h fallback, media valid, a signed media URL can be
   produced); media delivery through `MediaStore.signed_get_url(key, 7200)`; in `local` with
   LocalDiskStore and R2 configured, copy the asset to R2 at publish time and delete it when the
   container reaches FINISHED/ERROR/EXPIRED; without R2 the capability is `needs_media_host`;
   container creation up to 30 min before `planned_at` (carousel children first); status polling
   once a minute up to 5 minutes, then every 5 minutes up to the URL lifetime; `EXPIRED` recreate
   once; `media_publish` at `planned_at` after marking the point of no return; the 9007/2207027
   rule (read container; `PUBLISHED` -> find media id; else retry with the SAME `creation_id` every
   10 s up to 2 min; then `uncertain`; never a second container for a possibly-published target);
   verify with `GET /{media-id}?fields=id,permalink,media_type,timestamp`; reconcile per spec.
   Field rules: no `is_ai_generated` on carousel children, no `alt_text` on Reels/Stories.

3. **TikTok publishers (spec 08 §4).** `tiktok_inbox` for `tiktok_draft`: `POST
   /v2/post/publish/inbox/video/init/` with `FILE_UPLOAD`, chunk plan from the mediastore planner,
   sequential `PUT`s with `Content-Range` within the 1-hour URL validity (restart from init if it
   lapses), `SEND_TO_USER_INBOX` -> `needs_user`, creator posting -> `published`; respect 5 pending
   shares per 24 h (403 `spam_risk_too_many_pending_share` is final for today) and 6 requests per
   minute per token. `tiktok_direct` for `tiktok_video`, built and tested but offered only when
   `tiktok_app_status=audited` (spec 08 §4: never below audited). A route
   `GET /api/v1/accounts/{id}/tiktok/creator-info` that calls `creator_info` fresh each time (for the
   mandatory screen) and returns privacy options, interaction availability, max duration and the
   exact consent strings to display. Mandatory-screen rules enforced server-side: privacy chosen from
   the returned options with no default, interactions default off and disabled when unavailable,
   commercial content rules (branded content cannot be private), `is_aigc` from `ai_generated`,
   consent echoed in the release body; these choices are part of the payload hash and can only be
   made by a human session (403 `human_required` for keys). `post.publish.*` webhooks (via the
   realtime hook) and status-fetch polling (<= 30/min) drive state; map `fail_reason` per spec.

## Tests (fake HTTP servers on `httpx.MockTransport`; no real calls)

Instagram: Reel, image, carousel, story happy paths; 9007 after FINISHED then success with the same
creation_id and exactly one publish; container ERROR final; EXPIRED recreate once; reconcile
PUBLISHED/FINISHED/EXPIRED; no R2 -> `needs_media_host`; R2 temp copy deleted after the container
finishes; publishing limit reached blocks release. TikTok: inbox happy path to `needs_user` then
`published`; URL expiry mid-upload restarts from init; pending-share cap; direct publisher refused
below `audited`; key-initiated TikTok release -> 403 `human_required`; mandatory screen with no
privacy chosen -> cannot release; branded content + private -> validation error; rotated refresh
token persisted once under concurrent refresh.

## Done when

All built with tests; ruff, ruff format, mypy clean; the test scope in COMMON-RULES rule 7 passes;
`docs\PLATFORM_CAPABILITIES.md` regenerated. Report at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-social\REPORT.md` with every new route,
setting, and the exact live-test procedures for I-1..I-4 and T-1, T-2 the owner will run after
connecting.
