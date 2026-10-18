# Platform capability matrix

Status vocabulary (also used by `GET /api/platforms` and the Accounts page):

| State | Meaning |
| --- | --- |
| `implemented` | Runway code exists, is covered by offline tests, and is enabled when the account is connected with the required scopes. |
| `supported_not_implemented` | The official API documents the capability; Runway has not built it. |
| `approval_required` | The platform requires an app audit, review, or a prerequisite Runway cannot satisfy locally (for example public media hosting). |
| `unsupported` | No official API path exists. |
| `unverified` | Documentation is ambiguous or has not been checked against a live account. |

A capability is **ready** for a specific account only when its state is `implemented`, the account
status is `connected`, and every scope the capability needs was granted. The UI never shows an
enabled action for a capability that is not ready; it shows the reason and, where useful, a link
to the native platform.

"Live-verified" below means the capability was exercised against a real authorized account during
this work. Everything else is implemented against the documented response shapes with recorded
fixtures and has not been live-verified. See [`VERIFICATION_REPORT.md`](VERIFICATION_REPORT.md).

## YouTube

| Capability | State | Scopes | Notes and blockers |
| --- | --- | --- | --- |
| Account connection | implemented | `youtube.readonly`, `yt-analytics.readonly` (+ `youtube.force-ssl` when `RUNWAY_YOUTUBE_ENGAGE_SCOPES=true`) | Google OAuth 2.0 with PKCE; identity verified with `channels.list?mine=true`. Requires `RUNWAY_YOUTUBE_CLIENT_ID/SECRET` from a Google Cloud project with the YouTube Data API v3 and YouTube Analytics API enabled. Not live-verified. |
| Account analytics | implemented | `yt-analytics.readonly` | Daily `views`, `estimatedMinutesWatched`, `averageViewDuration`, `subscribersGained/Lost`, `likes`, `comments`, `shares` from `reports.query`; subscriber/view/video totals from `channels.list`. Recent days may be incomplete (platform delay). Revenue metrics not requested (needs `yt-analytics-monetary.readonly` and Partner Program membership). |
| Content inventory | implemented | `youtube.readonly` | Uploads playlist via `playlistItems.list` (1 unit/page). Shorts are labelled by the ≤180 s heuristic; the API has no Shorts flag. |
| Content analytics | implemented | `youtube.readonly` | `videos.list` statistics in batches of 50; per-sync snapshots build a history. |
| Comments (read) | implemented | `youtube.readonly` | `commentThreads.list` for the most recent N videos per sync (bounded). Videos with comments disabled are skipped. |
| Comments (reply) | implemented, interlocked | `youtube.force-ssl` | `comments.insert` (50 units). Requires `RUNWAY_INBOX_REPLIES_ENABLED=true`, the engage scope granted, and a per-reply confirmation. Verified by re-reading the reply. Not live-verified. |
| Publishing (videos) | supported_not_implemented | `youtube.upload` | `videos.insert` exists (100 inserts/day bucket). Not built in this release. |
| Publishing (Community posts) | unsupported by API | — | No Data API resource. The existing Runway assisted / authorized-browser Lineup path is unchanged. |
| Scheduling | supported_not_implemented | `youtube.upload` | `status.publishAt` on `videos.insert`. Community-post scheduling stays on the existing Lineup path. |
| Messaging | unsupported | — | — |

## Instagram (Instagram API with Instagram Login)

| Capability | State | Scopes | Notes and blockers |
| --- | --- | --- | --- |
| Account connection | implemented | `instagram_business_basic` | Business Login for Instagram OAuth, or import of a long-lived token generated in the Meta App Dashboard (Standard Access, own account). Professional (Business/Creator) account required. Requires `RUNWAY_INSTAGRAM_APP_ID/SECRET`. Not live-verified. |
| Account analytics | implemented | `instagram_business_manage_insights` | `followers_count`, `media_count` from `/me`; daily `reach` and `follower_count` plus totals (`views`, `profile_views`, `accounts_engaged`, `total_interactions`) from `/me/insights`. |
| Content inventory | implemented | `instagram_business_basic` | `/me/media` with cursor paging; Reels, images, carousels, videos, stories (while available). |
| Content analytics | implemented | `instagram_business_manage_insights` | `/{media}/insights`; metric set depends on media type; unsupported metrics are recorded as warnings. |
| Comments (read) | implemented | `instagram_business_manage_comments` | `/{media}/comments` with replies for the most recent N media per sync. |
| Comments (reply) | implemented, interlocked | `instagram_business_manage_comments` | `POST /{comment}/replies`, verified by re-reading. Requires `RUNWAY_INBOX_REPLIES_ENABLED=true`. Not live-verified. |
| Publishing | approval_required | `instagram_business_content_publish` | The API fetches media from a **public URL**; Runway hosts media on loopback only. Not possible from this app without a separate public media host. Manual workflow provided. |
| Scheduling | unsupported | — | The API has no native scheduling; an app must hold the post and publish at the time. Not built. |
| Messaging | supported_not_implemented | `instagram_business_manage_messages` | Not requested or built. |

## TikTok (Login Kit + Display API)

| Capability | State | Scopes | Notes and blockers |
| --- | --- | --- | --- |
| Account connection | implemented | `user.info.basic`, `user.info.profile`, `user.info.stats`, `video.list` | Login Kit (web). TikTok requires an **HTTPS** redirect URI, so local use needs `RUNWAY_OAUTH_REDIRECT_BASE_URL` pointing at a tunnel that rewrites the Host header to loopback. Requires `RUNWAY_TIKTOK_CLIENT_KEY/SECRET`. Not live-verified. |
| Account analytics | implemented (point-in-time only) | `user.info.stats` | `follower_count`, `following_count`, `likes_count`, `video_count`. No daily series exists in the Display API; trends come only from Runway's own sync snapshots and are labelled as such. |
| Content inventory | implemented | `video.list` | `/v2/video/list/` with cursor/`has_more`. Public videos only. |
| Content analytics | implemented | `video.list` | `view_count`, `like_count`, `comment_count`, `share_count` per video, snapshotted per sync. |
| Comments (read) | supported_not_implemented | TikTok for Business "Account Comment" | Only through the separate TikTok for Business Account API (`/business/comment/list/`), which needs a Business developer app and approvals. Inbox offers "Open on TikTok". |
| Comments (reply) | supported_not_implemented | TikTok for Business "Manage Account Comment" | Same as above. |
| Publishing | approval_required | `video.publish` | Content Posting API Direct Post; unaudited apps can only post privately. Not built. Manual workflow provided. |
| Scheduling | unsupported | — | No native scheduling in the Content Posting API. |
| Messaging | unsupported | — | Business Messaging API is for Business accounts via TikTok for Business. |

## How the matrix is enforced in code

- `runway.platforms.capabilities` holds the matrix as data; providers return it from
  `capabilities()`.
- `GET /api/platforms` and `GET /api/platforms/accounts` include `state`, `detail`, required scopes,
  and the per-account `ready` flag.
- Draft targets expose `publish_capability` so the Drafts UI renders a disabled Publish control
  with the exact reason and the manual "Mark as published" path.
- Inbox items expose `reply_capability` so the reply form is disabled (with a handoff link) unless
  the account is ready and the interlock is on.
- Tests assert that `ready` is never true for a disconnected account, a missing scope, or a
  non-implemented capability.
