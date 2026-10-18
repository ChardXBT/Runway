# Platform and competitor research (October 2026)

This document records the research that shaped Runway's creator-platform expansion. It separates
what the official documentation states from what Runway has verified against a live account, and
it records the exact sources so the claims can be re-checked when platform policies change.

Verification legend used throughout:

- **Documented** — stated in the linked official documentation on the date noted.
- **Implemented** — Runway code exists and is covered by offline tests against recorded response
  shapes.
- **Live-verified** — exercised against a real authorized account during this work. Unless a row
  says so explicitly, nothing in this document is live-verified; see
  [`VERIFICATION_REPORT.md`](VERIFICATION_REPORT.md).

## 1. Competitor landscape

Reviewed 2026-10-17 from public product pages. Prices are list prices on that date and change
often; they are recorded only to position Runway, not as a comparison table to maintain.

| Product | Structure | Creator-relevant strengths | Limits worth noting for Runway |
| --- | --- | --- | --- |
| [Buffer](https://buffer.com/pricing) | Create / Publish / Insights / Community (inbox) / Collaborate / Start Page / AI Assistant / API. Per-channel pricing: Free (3 channels, 10 scheduled posts each), Essentials $5/channel/mo, Team $10/channel/mo (yearly). | Channels include YouTube, TikTok, Instagram, Threads, Bluesky, Mastodon, Substack. Community inbox with reply, "comment score", saved replies, comment insights. "Made for creators" positioning. Machine-readable pricing for agents. | Cloud-only; creator data lives in Buffer. Per-channel pricing grows with each account. |
| [Metricool](https://metricool.com/pricing/) | Planner, Analytics, Reports, Inbox, Flows, SmartLinks, Competitors, Ads. Free (1 brand, 20 posts/month, 30 days of analytics), Starter from $20–25/mo, Advanced from $53–67/mo. | Deep analytics and reporting (PDF/PPT, Data Studio connector), competitor tracking, "best times", approval system, inbox. Supports YouTube, TikTok, Instagram, Twitch, Threads, Bluesky, Pinterest. | Brand-count pricing; X requires an add-on. Agency-oriented reporting is more than a single creator needs. |
| [Hootsuite](https://www.hootsuite.com/) | Enterprise/team social management: publishing, inbox, analytics, listening, employee advocacy. | Mature inbox and approval workflows. | Priced and designed for teams and agencies, not solo creators. (Public pricing not re-verified here.) |
| [Sprout Social](https://sproutsocial.com/) | Enterprise suite: publishing, Smart Inbox, analytics, listening, care. | Strongest inbox/case-management model in the category. | Enterprise pricing; heavy for a single creator. (Public pricing not re-verified here.) |
| [Vista Social](https://vistasocial.com/) | Publishing, inbox, analytics, reviews, listening; agency focus. | Broad network coverage and white-label reports. | Agency-first; not re-verified in detail. |
| [Later](https://later.com/) | Visual-first planner, link in bio, influencer marketing. | Instagram-centric visual calendar and media library. | Instagram-first heritage; creator-management features are secondary. Not re-verified in detail. |
| [Beacons](https://beacons.ai/) | Link-in-bio, store, email, media kit, invoicing for creators. | Monetization tooling for creators. | Not a publishing/analytics/inbox tool; adjacent category. |
| [Creator OS](https://www.creatoros.ca/) | Creator business operations (brand deals, invoicing, CRM). | Business-side workflows for creators. | Not a publishing/analytics/inbox tool; adjacent category. |
| [Postiz](https://github.com/gitroomhq/postiz-app) (AGPL-3.0) | Open-source scheduler and analytics; NestJS + Next.js + Prisma/PostgreSQL + Temporal + Redis. Cloud or self-hosted. | Transparent about the integration reality: self-hosting means "you create your own developer apps on each platform and go through their approval (Meta, YouTube, TikTok can take weeks)"; analytics "requires your own app credentials with analytics scopes". Public API, MCP, agent integrations. | Infrastructure-heavy for a local single-user app (Postgres, Redis, Temporal). AGPL — Runway does not copy its code. |
| [Mixpost](https://mixpost.app/) | Self-hosted Laravel package; dashboard, compose, calendar, queue, posts, media library, analytics, engagement inbox, connected accounts, templates. | Dashboard shows a "content runway", today's posts, best time to post, top posts to reuse. Engagement inbox labels comments by intent/sentiment with reply/like. | Paid self-hosted license for the full product; Laravel stack. Patterns observed, code not used. |

Patterns Runway adopts from the category (verified as common across the products above):

1. A home dashboard that answers "what is happening across my accounts" with per-platform cards,
   today's schedule, and recent interactions.
2. A connected-accounts page that shows identity, granted permissions, freshness, and
   reconnect/disconnect controls, and is honest when a platform app is not yet approved.
3. A single inbox that keeps comments, replies, messages, and mentions distinguishable.
4. A content library that links the published versions of one original piece of content.
5. Per-platform analytics with date ranges and content tables, not one blended score.

Patterns Runway deliberately avoids:

- Blended cross-platform "engagement scores" (also restricted by YouTube's developer policies,
  see below).
- Enterprise approval chains and team roles; Runway stays single-user and local.
- Cloud infrastructure (queues, Redis, hosted databases) that the local product does not need.

## 2. Official platform APIs

### 2.1 YouTube

Sources (read 2026-10-17):

- Data API reference: <https://developers.google.com/youtube/v3/docs/>
- Quota calculator: <https://developers.google.com/youtube/v3/determine_quota_cost>
- Analytics API channel reports: <https://developers.google.com/youtube/analytics/channel_reports>
- `reports.query`: <https://developers.google.com/youtube/analytics/reference/reports/query>
- Developer policies: <https://developers.google.com/youtube/terms/developer-policies>
- Policy compliance guide: <https://developers.google.com/youtube/terms/developer-policies-guide>

Documented facts that drive the implementation:

| Topic | Documented behaviour | Runway consequence |
| --- | --- | --- |
| Authorization | OAuth 2.0; scopes `youtube.readonly` (read channel/video/comment data), `yt-analytics.readonly` (Analytics reports), `yt-analytics-monetary.readonly` (revenue; channel must be in the YouTube Partner Program or the request returns 403), `youtube.force-ssl` (needed to insert comments/replies), `youtube.upload` (video uploads). | Default connection requests the two read-only scopes. `force-ssl` is requested only when `RUNWAY_YOUTUBE_ENGAGE_SCOPES=true`. Monetary and upload scopes are not requested in this release. |
| Quota | Default allocation: 10,000 units/day for most endpoints, plus **separate buckets of 100 calls/day each for `search.list` and `videos.insert`** (cost 1 per call in their own bucket). `channels.list`, `playlistItems.list`, `videos.list`, `commentThreads.list`, `comments.list` cost 1 unit; `comments.insert` costs 50. Resets at midnight Pacific. | Inventory uses `playlistItems.list` on the uploads playlist, never `search.list`. Runway estimates units per sync run and shows them. |
| Channel identity | `channels.list?mine=true&part=snippet,statistics,contentDetails` returns id, title, custom URL, thumbnails, subscriber/view/video counts, and the uploads playlist id. | Identity verification before any data is stored; the uploads playlist id is kept in account metadata. |
| Content inventory | `playlistItems.list` pages through the uploads playlist (50 per page); `videos.list` accepts up to 50 ids per call and returns `contentDetails.duration` (ISO 8601), `statistics`, `status.privacyStatus`. The API does not flag Shorts explicitly. | Runway labels items ≤ 180 s as "short" and documents that this is a heuristic, not a platform flag. |
| Analytics | `reports.query` with `ids=channel==MINE`, `dimensions=day`, metrics such as `views`, `estimatedMinutesWatched`, `averageViewDuration`, `subscribersGained`, `subscribersLost`, `likes`, `comments`, `shares`. Data is finalized with a delay (the product UI notes that recent days can be incomplete). | Daily series are stored with `source=youtube_analytics_api`; the UI labels the last days as possibly incomplete. |
| Comments | `commentThreads.list` by `videoId` or `allThreadsRelatedToChannelId`, with `replies`; `comments.insert` posts a reply (`snippet.parentId`). Videos with comments disabled return 403 `commentsDisabled`. | Reading is implemented. Replying is implemented behind an interlock and verified with `comments.list` after posting. |
| Community posts | No Community-post resource exists in the Data API. | Runway's existing assisted/authorized-browser Lineup path remains the only Community-post route. Unchanged. |
| Publishing/scheduling | `videos.insert` uploads a video; `status.publishAt` schedules a private video to go public. It needs `youtube.upload`, a local video file, and shares the 100/day insert bucket. | Officially supported, not implemented in this release. Drafts offer a manual workflow instead. |
| Data aggregation policy | Developer Policies III.E.2: API data may be aggregated only for channels under the same content owner and shown only to that owner; API clients must not combine API data from different content owners; must not intermix non-YouTube results and present them as YouTube data. | Runway never builds a blended cross-platform metric. YouTube numbers are always labelled with their source and shown separately from Instagram and TikTok numbers. |
| Credentials | Policies II.4: never request or store users' YouTube login credentials. | OAuth only. The existing Community connector uses the creator's own visible browser session, never a stored password. |

### 2.2 Instagram

Sources (read 2026-10-17):

- Platform overview: <https://developers.facebook.com/docs/instagram-platform/overview>
- Business Login for Instagram: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login>
- Content publishing: <https://developers.facebook.com/docs/instagram-platform/content-publishing>
- Rate limits: <https://developers.facebook.com/docs/graph-api/overview/rate-limiting>

| Topic | Documented behaviour | Runway consequence |
| --- | --- | --- |
| Two configurations | "Instagram API with Instagram Login" (professional account, no Facebook Page, host `graph.instagram.com`, permissions `instagram_business_*`) versus "Instagram API with Facebook Login" (Page-linked, host `graph.facebook.com`, permissions `instagram_*` + `pages_*`). | Runway implements **Instagram Login** only. It removes the Page-linking requirement and matches a creator account that lives on Instagram alone. |
| Account requirement | Professional account (Business or Creator). Personal accounts are not supported. | Shown in the Accounts UI before connecting. |
| Access levels | Standard Access serves accounts you own or manage and are added to the app; Advanced Access (App Review + Business Verification) is required to serve other people's accounts. For own accounts no login flow is strictly needed — a long-lived token can be generated in the App Dashboard. | Runway supports both the OAuth flow and importing a long-lived token (verified with `GET /me` before it is stored). |
| OAuth endpoints | Authorize `https://www.instagram.com/oauth/authorize`; exchange `POST https://api.instagram.com/oauth/access_token` (code valid 1 hour, single use, strip trailing `#_`); long-lived exchange `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token` (60 days); refresh `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token`. | Implemented exactly as documented; refresh runs before expiry during sync. |
| Permissions | `instagram_business_basic`, `instagram_business_manage_comments`, `instagram_business_manage_insights` (added 2025), `instagram_business_content_publish`, `instagram_business_manage_messages`. | Runway requests basic + comments + insights. Publishing and messaging permissions are not requested in this release. |
| Content | `GET /me/media` with `caption, media_type, media_product_type, media_url, permalink, thumbnail_url, timestamp, like_count, comments_count` and cursor paging. Insights per media (`/{media}/insights`) with metric sets that depend on media type. Account insights via `/me/insights` (`reach`, `follower_count` by day; totals such as `views`, `profile_views`, `accounts_engaged`). | Implemented. Unsupported metric for a media type is recorded as a warning, not a failure. |
| Comments | `/{media}/comments` with `replies`; `POST /{comment}/replies` to reply. | Reading implemented; reply implemented behind the interlock and verified by re-reading the new reply. |
| Publishing | Two-step container → `media_publish`. **Media must be hosted on a publicly accessible server at the time of publishing** ("we cURL your image using the passed in URL"). Limit: 100 API-published posts per 24-hour moving window (50 for carousels). JPEG only for images. | Runway binds to loopback and hosts no public media, so direct publishing is **not possible from this app** without a separate public media host. Drafts provide a manual workflow and a disabled Publish control with this exact reason. |
| Rate limits | Instagram Platform endpoints use Business Use Case limits: calls per app-user in a rolling 24 hours = 4800 × impressions on that account's content in the last 24 hours (messaging has separate per-second limits). | Sync is bounded per run and backs off on error code 4/17/32/613 or HTTP 429. |

### 2.3 TikTok

Sources (read 2026-10-17):

- Developer portal: <https://developers.tiktok.com/>
- Display API overview: <https://developers.tiktok.com/doc/display-api-overview>
- Login Kit (web): <https://developers.tiktok.com/doc/login-kit-web>
- Scopes: <https://developers.tiktok.com/doc/scopes-overview>
- Content Posting API: <https://developers.tiktok.com/doc/content-posting-api-get-started>
- TikTok API for Business reference (Business Account comments): <https://business-api.tiktok.com/portal/docs/api-reference/v1.3>

| Topic | Documented behaviour | Runway consequence |
| --- | --- | --- |
| Products are separate | Login Kit (OAuth), Display API (`/v2/user/info/`, `/v2/video/list/`, `/v2/video/query/`), Content Posting API (`video.publish`, `video.upload`), Research API (academic), Commercial Content API, and the separate **TikTok for Business** API (Business Account endpoints such as `/business/comment/list/`, `/business/comment/reply/create/`, `/business/video/list/`) with its own developer portal, app, and approvals. | Runway treats each as a distinct capability and never infers one from another. |
| Login Kit (web) | Redirect URIs must be absolute, begin with **`https`**, be static, and contain no fragment. Authorize `https://www.tiktok.com/v2/auth/authorize/` with `client_key`, comma-separated `scope`, `redirect_uri`, `state`, `response_type=code`; token `POST https://open.tiktokapis.com/v2/oauth/token/` (form-encoded) returning `access_token`, `expires_in`, `open_id`, `refresh_token`, `refresh_expires_in`, `scope`. | A loopback `http://127.0.0.1` callback is rejected by TikTok. Local use requires `RUNWAY_OAUTH_REDIRECT_BASE_URL` pointing at an HTTPS tunnel that rewrites the Host header to the loopback host (Runway's request guard only serves loopback hosts and is not relaxed). Documented in the setup guide. |
| Display API | Scopes `user.info.basic` (default), `user.info.profile`, `user.info.stats` (follower/likes/video counts), `video.list` (public videos with `view_count`, `like_count`, `comment_count`, `share_count`, `create_time`, `share_url`, `cover_image_url`); cursor + `has_more` paging; `max_count` up to 20. Private videos are not returned. | Implemented for identity, profile stats, inventory, and per-video counts. No per-day analytics exist in the Display API; the trend chart for TikTok is built only from Runway's own periodic snapshots and labelled as such. |
| Comments | Not part of the Display API. Reading/replying is only available through the TikTok for Business Account API, which requires a Business developer app and the "Account Comment" permission approvals. | `comments_read` and `comments_reply` are **supported_not_implemented**. The inbox offers an "Open on TikTok" handoff. |
| Publishing | Content Posting API: Direct Post requires the `video.publish` scope and app approval; "All content posted by unaudited clients will be restricted to private viewing mode" until the API client passes an audit. `FILE_UPLOAD` from local files is supported (chunked PUT), `PULL_FROM_URL` requires a verified domain. | **approval_required**; not implemented in this release. Drafts provide the manual workflow. |
| Messaging | Business Messaging API is for TikTok Business accounts through TikTok for Business. | unsupported in Runway. |

## 3. Cross-cutting constraints

- **Local-first OAuth.** Google accepts loopback redirect URIs for development; Meta accepts
  `localhost`/loopback redirect URIs while an app is in development mode; TikTok requires HTTPS.
  Runway keeps a single configurable redirect base (`RUNWAY_OAUTH_REDIRECT_BASE_URL`).
- **Public media.** Instagram publishing and TikTok `PULL_FROM_URL` need publicly reachable media.
  Runway hosts media on loopback only, so those publishing paths are out of scope until a public
  media host exists.
- **No blended metrics.** Each number keeps its platform, metric definition, source, and
  collection time. This is both a product decision and a YouTube policy requirement.
- **Reading is not permission to write.** Every write capability (reply, publish) is a separate
  capability with its own scope, interlock, confirmation, and verification step. Runway never
  reports success for an external action it has not re-read from the platform.
- **Shared responsibility with Qlob Manager.** The private Qlob Manager project already monitors
  and replies to comments for the Qlob accounts on an autonomous schedule. Runway's inbox is
  read-first and reply-by-explicit-action; it is not a second responder. Runway does not read
  Qlob Manager's private SQLite databases and does not import its interaction history.

## 4. Concepts reused from Qlob Manager (design level only)

Inspected read-only on 2026-10-17. No code, data, or credentials were copied.

- Platform-scoped external identities for comments and posts to prevent collisions.
- An append-only action ledger with a forward-only state machine
  (proposed → attempted → completed → verified; failed/uncertain terminal until reconciled).
- "Reconcile before retry": an ambiguous external outcome is never retried blindly.
- Identity verification before any write (the authenticated account must match the connected
  account).
- Secret-field rejection in anything that is persisted or logged.
- Quota accounting per API call.
