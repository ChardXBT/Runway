# TikTok — facts for connection, publishing and sync

All pages read 2026-11-14 through a fetch-and-summarize tool (TikTok blocks the other scraper).
Wording in quotes is as returned by that tool; re-read the page before quoting it to TikTok.

Sources:

- Content Sharing Guidelines: <https://developers.tiktok.com/doc/content-sharing-guidelines>
- Content Posting API get started: <https://developers.tiktok.com/doc/content-posting-api-get-started>
- Direct Post reference: <https://developers.tiktok.com/doc/content-posting-api-reference-direct-post>
- Upload (inbox) reference: <https://developers.tiktok.com/doc/content-posting-api-reference-upload-video>
- Media transfer guide: <https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide>
- Get post status: <https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status>
- Token management: <https://developers.tiktok.com/doc/oauth-user-access-token-management>
- Webhook events: <https://developers.tiktok.com/doc/webhooks-events>
- Webhook verification: <https://developers.tiktok.com/doc/webhooks-verification>
- Sandbox: <https://developers.tiktok.com/doc/add-a-sandbox>
- App review guidelines: <https://developers.tiktok.com/doc/app-review-guidelines>

## 1. The rule that decides the strategy

Content Sharing Guidelines, intended use for the Direct Post audit:

- [Official] Acceptable: apps that help authentic creators post original content.
- [Official] Not acceptable: apps that copy arbitrary content from other platforms; "utility
  tools that upload content to accounts you or your team manage"; test applications or apps
  "limited to internal groups or private use". Clients should be intended for a wide audience.

App review guidelines, rejection reasons:

- [Official] "Apps must not be built for private or personal use." Apps "still in development or
  testing will not be approved."
- [Official] The website must be a real, externally facing site ("not a landing or login
  page") with Privacy Policy and Terms of Service links visible without opening a menu. The
  demo video must show the full flow on the same domain as the registered website.

**Consequence:** an owner-only Runway cannot obtain public Direct Post through its own TikTok
app. Public posting through Runway's own app becomes possible only once Runway is a genuinely
public product with a real website, and passes the audit.

## 2. Unaudited clients

- [Official] All content is restricted to `SELF_ONLY`.
- [Official] At most 5 users can post per 24 hours.
- [Official] The posting account must itself be set to private at posting time. The API returns
  `unaudited_client_can_only_post_to_private_accounts` otherwise.
- [Official] To make such a post public later the owner must make the account public, then
  change each post's privacy by hand.

For a public creator account this path is useless.

## 3. Three posting paths

| | Direct Post | Upload to inbox (draft) | Photo post |
| --- | --- | --- | --- |
| Endpoint | `POST /v2/post/publish/video/init/` | `POST /v2/post/publish/inbox/video/init/` | `POST /v2/post/publish/content/init/` |
| Scope | `video.publish` | `video.upload` | per `post_mode` |
| Result | Posted by the API | Notification in the creator's TikTok inbox; creator finishes and posts in the app | Photo carousel |
| Media source | `FILE_UPLOAD` or `PULL_FROM_URL` | same | `PULL_FROM_URL` only |
| Init rate limit | 6 per minute per user token | 6 per minute per user token | not read |
| Per-creator cap | about 15 posts per 24 h, shared across all Direct Post clients | 5 pending shares per 24 h | not read |

All hosts are `https://open.tiktokapis.com`.

- [Official] The inbox reference page "does not mention any restriction for unaudited clients".
  [Unverified] Whether inbox uploads work from an unaudited or sandbox app is the most valuable
  thing to test (acceptance test T-2).
- [Official] Sandbox "does not offer access to Content Posting API for public videos".

## 4. Direct Post details

Before every post call `POST /v2/post/publish/creator_info/query/`. It returns
`creator_avatar_url`, `creator_username`, `creator_nickname`, `privacy_level_options`,
`comment_disabled`, `duet_disabled`, `stitch_disabled`, `max_video_post_duration_sec`.

`post_info` fields:

| Field | Rule |
| --- | --- |
| `privacy_level` | Required. `PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`. Must be one of the creator's `privacy_level_options`. |
| `title` | The caption. Max 2,200 UTF-16 runes. |
| `disable_duet`, `disable_stitch`, `disable_comment` | Booleans. |
| `video_cover_timestamp_ms` | Cover frame; first frame if unset. |
| `brand_content_toggle` | Paid partnership for a third party. |
| `brand_organic_toggle` | Promoting the creator's own business. |
| `is_aigc` | Adds the "AI-generated" label. |

`source_info`: `source` = `FILE_UPLOAD` (with `video_size`, `chunk_size`, `total_chunk_count`)
or `PULL_FROM_URL` (with `video_url`). Response: `publish_id`, and `upload_url` for file upload
(valid one hour).

Errors: `spam_risk_too_many_posts`, `spam_risk_user_banned_from_posting`,
`reached_active_user_cap`, `unaudited_client_can_only_post_to_private_accounts`,
`url_ownership_unverified`, `privacy_level_option_mismatch`, `access_token_invalid`,
`scope_not_authorized`, `rate_limit_exceeded`.

## 5. Media transfer

FILE_UPLOAD:

- [Official] Chunks are 5 MB to 64 MB; the final chunk may be up to 128 MB. Files under 5 MB go
  in one request. At most 1,000 chunks, uploaded in order.
- [Official] `total_chunk_count` = `video_size` ÷ `chunk_size`, rounded down.
- [Official] `PUT` each chunk to `upload_url` with `Content-Type`, `Content-Length`,
  `Content-Range: bytes FIRST-LAST/TOTAL`. `206` means more expected, `201` means complete.
  `416` means the range does not match progress. `403` means the URL expired.

PULL_FROM_URL:

- [Official] The URL must be on a domain or URL prefix verified in the developer portal, use
  HTTPS, and not redirect. Download times out after one hour.

Limits:

- [Official] Video: MP4 (recommended), WebM, MOV; H.264 (recommended), H.265, VP8, VP9; 23–60
  fps; each side 360–4096 px; max 4 GB; duration up to the creator's
  `max_video_post_duration_sec`, at most 10 minutes through the API.
- [Official] Photos: WebP or JPEG, max 1080p, max 20 MB each.

**Consequence:** use FILE_UPLOAD for video. It needs no public URL and no verified domain.
Photo posts need a verified domain; defer them.

## 6. Status and webhooks

- [Official] `POST /v2/post/publish/status/fetch/` (30 per minute per token). Statuses:
  `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`, `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE`,
  `FAILED`. `publicaly_available_post_id` (TikTok's spelling) appears only once the post is
  public and has passed moderation.
- [Official] `fail_reason` values: `file_format_check_failed`, `duration_check_failed`,
  `frame_rate_check_failed`, `picture_size_check_failed`, `internal` (retryable),
  `video_pull_failed`, `photo_pull_failed`, `publish_cancelled`, `auth_removed`,
  `spam_risk_too_many_posts`, `spam_risk_user_banned_from_posting`, `spam_risk_text`,
  `spam_risk`.
- [Official] Webhook events: `post.publish.failed`, `post.publish.complete`,
  `post.publish.inbox_delivered`, `post.publish.publicly_available`,
  `post.publish.no_longer_publicaly_available`, and `authorization.removed` (reasons 0–5:
  unknown, user disconnected, account deleted, age changed, account banned, developer revoked).
- [Official] Signature: header `TikTok-Signature: t=<timestamp>,s=<signature>`. Signature is
  HMAC-SHA256 over `<timestamp>.<raw body>` keyed with the app's `client_secret`. Check the
  timestamp against the clock; no tolerance is specified.
- [Official] One callback URL per app; subscribed to all events by default.

## 7. Required posting screen (audit requirement)

The screen must:

1. Show the creator's nickname, fetched fresh each time.
2. Let the user enter or edit the title.
3. Offer privacy options only from `privacy_level_options`, in a dropdown with **no default**.
4. Offer comment, duet and stitch toggles, **all off by default**, greyed out when the creator
   has them disabled. Photo posts show only comments.
5. Offer a commercial-content toggle, off by default. When on, show "Your brand" and "Branded
   content"; at least one must be ticked or Publish is disabled with the hint "You need to
   indicate if your content promotes yourself, a third party, or both."
6. Disable "only me" when Branded content is ticked ("Branded content visibility cannot be set
   to private.").
7. Show the consent line: "By posting, you agree to TikTok's Music Usage Confirmation", or with
   branded content, "By posting, you agree to TikTok's Branded Content Policy and Music Usage
   Confirmation."
8. Show a preview, and tell the user processing can take a few minutes.
9. Add no watermark, logo, link or promotional text to the content.
10. Send nothing until the user has expressly consented.

**Consequence for agents:** an agent cannot choose privacy or interaction settings on the
user's behalf for TikTok. Muse can prepare a draft; a human completes this screen.

## 8. Tokens and sandbox

- [Official, as read] Access token 2 hours (`expires_in`), refresh token 180 days
  (`refresh_expires_in`). "A refresh request will return a different `refresh_token` from the
  one you sent." Reusing the old one fails and forces re-authorization. I recalled different
  lifetimes from older documentation, so **read `expires_in` from the response and never
  hard-code a lifetime**.
- [Official] Sandbox: up to 5 sandboxes per app, up to 10 owned TikTok accounts as target users
  each, no review needed.

## 9. Reading data

- Display API (`user.info.*`, `video.list`): point-in-time counts only, public videos only. No
  daily series, no comments. Unchanged from the earlier research.
- Comments and richer insights sit in the separate TikTok API for Business, with its own app
  and approvals. Not re-researched in depth.

## 10. United States joint venture

- [Official] TikTok's US Terms of Service now name "TikTok USDS Joint Venture LLC" as the US
  operator. Reuters reported the deal closing on 2026-01-22.
- [Unverified] I found no documentation of a separate developer platform or API for US accounts.
  TikTok's developer changelog reportedly has only data-portability entries since June 2026
  [Community]. Absence of evidence only. The owner is in Canada, which is outside the joint
  venture's scope.

## 11. Could not determine

- Whether inbox upload works for unaudited or sandbox apps.
- Whether a draft-only app can pass app review, given the personal-use rule.
- Audit duration.
- PKCE and redirect URI rules for web (page not read today).
- Webhook retry policy and the response TikTok expects.
- Whether the Business API is open to a creator (non-business) account.
