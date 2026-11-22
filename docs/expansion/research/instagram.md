# Instagram — facts for connection, publishing and sync

All pages read 2026-11-14. Meta moved its docs from `/docs/…` to `/documentation/…`; old links
redirect, and some sub-pages return 404 at the old path.

Sources:

- Overview: <https://developers.facebook.com/documentation/instagram-platform/overview>
- Content publishing: <https://developers.facebook.com/documentation/instagram-platform/content-publishing>
- IG User media reference: <https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media>
- Webhooks setup: <https://developers.facebook.com/documentation/instagram-platform/webhooks/setup>
- Unified Login migration guide: <https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/migration-guide>
- Business Verification: <https://developers.facebook.com/documentation/development/release/business-verification>
- Access Verification: <https://developers.facebook.com/documentation/development/release/access-verification>

## 1. Access levels

- [Official] Standard Access is the default. "If your app only serves your Instagram
  professional account or an account you manage, Standard Access is all your app needs."
- [Official] Advanced Access is required when the app serves accounts "you don't own or manage".
  It needs Meta App Review, and Business Verification.
- [Official] Business Verification: required since 2023-02-01 for advanced access or when the
  app "allows other Businesses to access their own data". Not needed "if your app will only be
  used by app users who have a role on the app itself".
- [Official] "Access Verification is no longer required." Meta now relies on Business
  Verification and App Review.
- [Official] Up to 50 accounts can be added as Instagram Testers on an app.
- **Consequence:** the owner can publish to their own account today with no review. Hosted
  multi-workspace needs App Review plus Business Verification.

## 2. Publishing flow

- [Official] Two steps: `POST /<IG_ID>/media` creates a container; `POST /<IG_ID>/media_publish`
  with `creation_id` publishes it. Host is `graph.instagram.com` for Instagram Login.
- [Official] "We cURL media used in publishing attempts, so the media must be hosted on a
  publicly accessible server at the time of the attempt." This applies to `image_url`,
  `video_url` and `cover_url`.
- [Official] Resumable upload (`upload_type=resumable`, host `rupload.facebook.com`) exists but
  the page says "Only for apps that have implemented Facebook Login for Business". The table
  also lists it under Unified Login. It is **not** available to Runway's current Instagram
  Login integration.
- [Official] Container status: `GET /<CONTAINER_ID>?fields=status_code` returns `EXPIRED`,
  `ERROR`, `FINISHED`, `IN_PROGRESS` or `PUBLISHED`. Poll "once per minute, for no more than 5
  minutes".
- [Official] Containers expire after 24 hours. An account can create 400 containers per rolling
  24 hours.
- [Official] No scheduling parameter exists. The app must hold the post and publish on time.
- [Official] URLs should use only US-ASCII characters "or the request will fail".

Container parameters (Instagram Login unless noted):

| Parameter | Use |
| --- | --- |
| `image_url` | Image posts. JPEG only. |
| `video_url` + `media_type=REELS` | Reels. |
| `media_type=STORIES` | Stories, image or video. No stickers. |
| `media_type=CAROUSEL` + `children` | Up to 20 items, each created with `is_carousel_item=true`. |
| `caption` | Max 2,200 characters, 30 hashtags, 20 @ tags. Not on carousel children. |
| `share_to_feed` | Reels: also show in Feed. |
| `cover_url`, `thumb_offset` | Reel cover. `cover_url` wins if both are sent. |
| `audio_name` | Reels, can be set once. |
| `collaborators` | Up to 3 usernames. Not Stories. |
| `user_tags`, `location_id` | Tagging. |
| `alt_text` | Images only, up to 1,000 characters (added 2025-03-24). |
| `trial_params.graduation_strategy` | Trial reels: `MANUAL` or `SS_PERFORMANCE`. |
| `is_ai_generated` | Self-disclosure of AI use. Not on carousel children. |
| `is_paid_partnership`, polls, comment prompts, `product_tags` | Facebook Login only. |

## 3. Limits

- [Official] "Instagram accounts are limited to 100 API-published posts within a 24-hour moving
  period." "Carousels count as a single post."
- [Official] The same page, under carousels, still says "Accounts are limited to 50 published
  posts within a 24-hour period." The two lines conflict.
- [Official] `GET /<IG_ID>/content_publishing_limit` returns current usage. **Use this as the
  source of truth** and assume 50 until it says otherwise.
- [Official] General rate limit: calls per 24 hours = 4,800 × impressions.

## 4. Media specifications

| | Image | Reel | Story video |
| --- | --- | --- | --- |
| Container | JPEG | MOV or MP4, moov atom at front, no edit lists | same |
| Size | 8 MB | 300 MB | 100 MB |
| Duration | — | 3 s to 15 min | 3 s to 60 s |
| Video codec | — | H.264 or HEVC, progressive, closed GOP, 4:2:0 | same |
| Audio | — | AAC, ≤ 48 kHz, mono or stereo, 128 kbps | same |
| Frame rate | — | 23–60 fps | same |
| Width | 320–1440 px | ≤ 1920 px | ≤ 1920 px |
| Aspect ratio | 4:5 to 1.91:1 | 0.01:1 to 10:1, 9:16 recommended | 0.1:1 to 10:1, 9:16 recommended |
| Bitrate | — | VBR, ≤ 25 Mbps | same |

- [Official] Carousel images are cropped to the first image's aspect ratio.
- [Inference] The 300 MB Reel limit is far below YouTube's and TikTok's. One source file may be
  valid for two platforms and not the third; validation must be per target.

## 5. Webhooks

- [Official] "Your app must be published or Live to receive notifications for app users."
- [Official] "Advanced Access is required for `comments` and `live_comments` notifications."
  The setup table lists Advanced Access for the Instagram Login flavour in general and
  "Business verification: required for production access".
- [Official] The account that owns the media must be public.
- [Official] Fields are chosen per app, not per account.
- [Official] Subscribe each account: `POST https://graph.instagram.com/v27.0/<IG_ID>/subscribed_apps?subscribed_fields=…`.
- [Official] Verification: `GET` with `hub.mode`, `hub.challenge`, `hub.verify_token`; echo the
  challenge. Payloads are signed in `X-Hub-Signature-256`.
- [Official] Up to 1,000 updates per batch. On failure Meta retries "immediately, then … with
  decreasing frequency over the next 36 hours", then drops.
- **Consequence:** real-time Instagram comments are not available to an owner-only install on
  Standard Access. Polling stays the mechanism until App Review passes.
- [Unverified] No webhook announces that a new media item was published elsewhere. Inventory
  stays on polling.

## 6. Unified Login (new)

- [Official] "The Instagram API with Unified Login lets your clients connect their Instagram
  professional account by logging in with Instagram or Facebook, without a Facebook Page. New
  Instagram API features ship only on Unified Login."
- [Official] "Do you have to move now? No. Your existing integrations will continue to work.
  There is no end date yet."
- [Official] What changes: host becomes `graph.facebook.com`; `/me` is replaced by the account
  id from `GET /me/assigned_instagram_accounts`; the token is a Business Integration System User
  Access Token; permission names drop `_business` (`instagram_content_publish`, and so on);
  login uses a `config_id` Login URL; webhooks follow the Facebook Login payload format.
- [Official] A Unified Login token stops working when the client removes the app from their
  business portfolio or moves the Instagram account to another portfolio.
- [Inference] The content-publishing page lists `rupload.facebook.com` under Unified Login, so
  migrating would probably unlock upload-from-disk for video and remove the public-URL need for
  Reels. The migration guide does not say so explicitly. Test before relying on it.

## 7. Could not determine

- Whether a sole developer with no registered company can complete Business Verification. The
  developer page points to the Business Help Center for the document list; I did not find a
  clear answer. Community reports conflict.
- App Review duration and rejection patterns for `instagram_business_content_publish`.
- Whether Instagram's fetcher accepts Cloudflare R2 signed URLs (query-string signatures on the
  `r2.cloudflarestorage.com` host). Nothing official forbids it.
- Token lifetime details for Instagram Login. The Business Login page did not render its
  details to my tool. Runway's code assumes 60-day long-lived tokens refreshed before expiry,
  from the earlier research; unconfirmed today.
- Whether published media can be edited or deleted through the API.
- Current valid insight metric names after Meta's 2025 changes.
- Graph API version lifetime. Examples on 2026-11-14 use `v27.0`.
