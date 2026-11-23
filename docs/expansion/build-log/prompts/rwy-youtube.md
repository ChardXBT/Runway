Job name: **rwy-youtube**

Job rwy-youtube: build the real YouTube publishers (phase P3.1 plus the Community-post API adapter)
and production-ready YouTube connection for the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-youtube`,
branch `expansion/k-youtube`, based on `expansion/build`, which already contains P0, the shared
v1 contracts, the media core, the publishing engine (Publisher port, assisted and mock adapters,
worker handlers, `/api/v1/posts`), real-time sync and the agent interface. It has its own `.venv`.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read the reports `rwy-p0\REPORT.md`, `rwy-media\REPORT.md`, `rwy-engine\REPORT.md`
and `rwy-realtime\REPORT.md` in `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\`, then in your
worktree `docs\expansion\specs\00-principles.md`, `docs\expansion\specs\03-publishing-engine.md`,
`docs\expansion\specs\08-platform-adapters.md` §1, §2 and §5, `docs\expansion\research\youtube.md`
and `docs\expansion\research\recheck-20261122.md` §1, and the existing
`src\runway\platforms\youtube.py` provider.

Another job (`rwy-social`, Instagram and TikTok publishers) and the web UI job (`rwy-web`) run at
the same time. Touch only YouTube files plus one registration line per adapter; the UI job builds
screens against the API you expose, so document every new route and payload in the report.

## Work packages

1. **Connection (spec 08 §1-§2).** Incremental consent: a route
   `POST /api/v1/accounts/{id}/publishing/enable` that starts an OAuth flow adding
   `youtube.upload` (and `youtube.force-ssl` when the `youtube_post` adapter is enabled) with
   `include_granted_scopes=true`, `access_type=offline`, `prompt=consent`, through the existing
   state-row machinery. After every exchange and refresh store the granted scopes and recompute
   capability. Refuse to attach a token whose `channels.list?mine=true` channel id differs from the
   account being reconnected. Identity check before every write.

2. **`youtube_direct` publisher** for `youtube_video` and `youtube_short`, exactly spec 08 §2 "Send":
   preflight (scope, per-Google-project upload counter below 100 per Pacific day, file readable,
   settings valid including explicit privacy, `made_for_kids` and `has_paid_product_placement`);
   resumable session with `part=snippet,status` (and `paidProductPlacementDetails`); `native`
   scheduling sends `privacyStatus=private` + `publishAt`; 8 MB chunks via the mediastore iterator,
   persisting session URI and confirmed offset on the attempt after each chunk; point of no return
   marked before the final byte range; `processing` polling with `videos.list` until processed or
   failed/rejected; `thumbnails.set` after processing with 403 as a warning; verify by reading back
   title, privacy, `publishAt`, paid-placement flag. Reconcile exactly as spec 08 §2 (session query
   `Content-Range: bytes */N`: 308 resume, 404 then match newest uploads by title + duration within
   the attempt window; one match published, none failed_retryable, several uncertain with the
   candidates shown). Map the named errors (`uploadLimitExceeded`, `quotaExceeded`,
   `invalidPublishAt`, `forbiddenPrivacySetting`, `invalidTitle`, `invalidDescription`,
   `invalidTags`, `invalidCategoryId`) to final/retryable with user messages. Category list from
   `videoCategories.list`, cached, exposed at `GET /api/v1/accounts/{id}/youtube/categories`.

3. **`youtube_post` publisher** (Community posts API pilot) exactly as the "Community posts"
   section of spec 08 §2: gated by `youtube_posts_api_access` (default false -> capability
   `needs_app_approval`), image upload to
   `https://www.googleapis.com/upload/youtube/v3/posts/image?channelId=…`, `posts.insert` with
   `part=snippet,attachments`, held scheduling only, verify via `posts.list`, reconcile by matching
   newest posts. When access is on, an accepted Lineup proposal can be sent by API through the
   cross-post bridge as an alternative to the browser publisher; never both for one proposal (add a
   guard and a test). The browser publisher stays the default and unchanged.

4. **WebSub hook-up**: when a YouTube target reaches `published`, boost inventory polling (the
   realtime `boost` function) so verify and import happen fast.

## Tests (all against a fake YouTube HTTP server built on `httpx.MockTransport`; no real calls)

Full happy path for video and Short (native and held); resume after a simulated crash mid-upload
produces one video (test Y-5 offline equivalent); 404 session -> reconcile match / no match /
ambiguous; read timeout on the final chunk -> `uncertain`, no retry; upload counter blocks the
101st upload of the Pacific day and resets at Pacific midnight (DST-safe); thumbnail 403 is a
warning; privacy, made-for-kids or paid-placement missing -> cannot release; wrong-channel token
refused; `youtube_post` disabled -> `needs_app_approval`; enabled -> image upload + insert + verify;
never both browser publisher and API for one proposal.

## Done when

All built with tests; ruff, ruff format, mypy clean; the test scope in COMMON-RULES rule 7 passes;
`docs\PLATFORM_CAPABILITIES.md` regenerated. Report at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-youtube\REPORT.md` with every new route,
setting and the exact live-test procedure for Y-1..Y-5 the owner will run after connecting.
