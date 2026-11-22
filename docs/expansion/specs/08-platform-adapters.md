# Spec 08 — Platform adapters

Decision D10 and the per-platform detail for specs 02–04. Facts and sources are in
[`../research/youtube.md`](../research/youtube.md),
[`../research/instagram.md`](../research/instagram.md) and
[`../research/tiktok.md`](../research/tiktok.md). Where a fact is marked unverified there, the
adapter must be written so that being wrong is visible, not silent.

Each adapter implements the `Publisher` port (spec 03 §5) and lives beside the existing provider
in `src/runway/platforms/`. Providers keep reading; publishers write. They share the token and
HTTP helpers (`provider_request`, `safe_error_summary`, `request_may_have_been_delivered`).

## 1. Connection changes common to all

- Request publishing scopes **only when the user turns publishing on** for that account
  (incremental consent), not at first connect. Reason: fewer scary permissions up front, and
  each platform reviews scope use.
- After every token exchange and refresh, store the scopes actually granted and recompute
  capability. A user can untick a scope on Google's screen.
- Identity check before every write: the authenticated account id must equal the connected
  account id (already done for reads).
- Fix refresh handling as in spec 04 §3 before adding any write.

## 2. YouTube

### Connect

- Add scope `https://www.googleapis.com/auth/youtube.upload` when publishing is enabled, with
  `include_granted_scopes=true`, `access_type=offline`, `prompt=consent`.
- Owner install: set the Google project's publishing status to "In production" to avoid 7-day
  refresh tokens (research/youtube §4; verify with test Y-1).
- A Google account can hold several channels. Keep verifying with `channels.list?mine=true` and
  refuse to attach a token whose channel id differs from the account being reconnected.

### Target settings (`youtube_video`, `youtube_short`)

| Field | Rule |
| --- | --- |
| `title` | Required, ≤ 100 characters [working knowledge; the API returns `invalidTitle` otherwise] |
| `description` | ≤ 5,000 characters [same] |
| `tags[]` | Optional |
| `category_id` | Required by Runway; choices from `videoCategories.list`, cached |
| `privacy` | Required: `public`, `unlisted` or `private`. No default. |
| `made_for_kids` | Required boolean (`status.selfDeclaredMadeForKids`). No default. |
| `contains_synthetic_media` | Boolean; defaults from the post's `ai_generated` |
| `notify_subscribers` | Default true |
| `embeddable`, `license` | Optional |
| `schedule_mode` | `native` or `held` (spec 03 §6) |
| `thumbnail_asset_id` | Optional |

`youtube_short` is the same call. Runway validates that the file is vertical or square and
within the Shorts length limit and labels the result a heuristic, because the API has no Shorts
flag.

### Send

1. Preflight: scope present; project upload counter below 100 for the Pacific day; file readable.
2. Start a resumable session with `part=snippet,status` and the metadata. With
   `schedule_mode=native`, send `privacyStatus=private` and `publishAt`.
3. Upload in 8 MB chunks, persisting the session URI and confirmed offset on the attempt after
   each chunk.
4. Point of no return: the request carrying the final byte range. Mark `request_sent_at` first.
5. `201` gives the video id: state `processing`.
6. Poll `videos.list?part=status,processingDetails` (1 unit) with backoff until
   `uploadStatus=processed`, or `failed`/`rejected` (final failure with the platform's reason).
7. If a thumbnail was chosen, call `thumbnails.set` after processing. A `403` here is recorded
   as a warning on the target ("thumbnail not accepted for this channel"), not a failed post.
8. Verify: read back title, privacy, `publishAt`; compare; state `verified`.

Reconcile: query the session (`Content-Range: bytes */N`). `308` means incomplete and safe to
resume. `404` means the session expired: list the newest uploads and match by title and
duration within the attempt's time window. One match: `published`. None: `failed_retryable`.
Several or ambiguous: stay `uncertain` and show them to the human.

Errors to map by name: `uploadLimitExceeded` (final for today; reschedule suggestion),
`quotaExceeded` (retry after Pacific midnight), `invalidPublishAt`, `forbiddenPrivacySetting`,
`invalidTitle`, `invalidDescription`, `invalidTags`, `invalidCategoryId`.

### Community posts

Unchanged. No API exists. The assisted and authorized-browser Lineup paths stay as they are and
remain local-only.

## 3. Instagram

### Flavour

Stay on **Instagram API with Instagram Login** now. Add **Unified Login** as a second
connection adapter before the hosted launch.

Why not migrate immediately: the existing integration works for the owner's account with no
review; Meta states no end date; migrating now would delay the owner's publishing for a login
flow whose main benefits (new features, probably upload-from-disk) are not needed yet.
Why not ignore it: Meta says new features ship only on Unified Login, and hosted users will
expect the newer login. Both adapters satisfy one internal interface that hides host, token
type and the `/me` versus account-id difference, which is exactly what the migration guide says
changes.

### Connect

- Add `instagram_business_content_publish` when publishing is enabled.
- Owner: Standard Access; the account must have a role on the Meta app or be added as an
  Instagram Tester.
- Hosted: blocked until `instagram_app_status=live_advanced` (spec 05 §4).

### Target settings

| Post type | Fields |
| --- | --- |
| `instagram_reel` | `caption` (≤ 2,200 characters, ≤ 30 hashtags, ≤ 20 mentions), `share_to_feed`, `cover_asset_id` or `thumb_offset_ms`, `audio_name`, `collaborators[≤3]`, `trial` (`MANUAL` or `SS_PERFORMANCE`), `is_ai_generated` |
| `instagram_image` | `caption`, `alt_text` (≤ 1,000), `user_tags`, `location_id`, `collaborators`, `is_ai_generated` |
| `instagram_carousel` | 2–20 children (images or videos), `caption` on the parent only |
| `instagram_story` | image or video; no caption, no stickers |

Instagram has no privacy field; the post inherits the account's visibility. The release dialog
says so explicitly ("visible to everyone who can see this account").

### Send

1. Preflight: scope; `GET /{ig-id}/content_publishing_limit` shows room; media valid; a signed
   media URL can be produced (spec 02 §6). Count against 50 per 24 h unless the endpoint reports
   a higher quota.
2. Create the container up to 30 minutes before `planned_at`. For carousels create each child
   first, then the parent.
3. Poll `status_code` once a minute. `FINISHED`: ready. `ERROR`: final failure; fetch the
   container's error detail. `EXPIRED`: recreate once.
4. At `planned_at`: mark `request_sent_at`, call `media_publish` with `creation_id`. This is
   the point of no return.
5. The response gives the media id: `published`.
6. Verify with `GET /{media-id}?fields=id,permalink,media_type,timestamp`. Store the permalink.
7. Delete the temporary R2 copy if one was made.

Reconcile: read the container. `PUBLISHED`: find the media id from the newest media items.
`FINISHED`: it was not published; safe to retry. `EXPIRED` or `ERROR`: not published.

Known hazards to surface clearly: aspect ratio or codec rejections arrive as container `ERROR`,
not at creation; a URL with non-ASCII characters fails; the account must be professional.

### Later

Comment webhooks after App Review (spec 04). Upload-from-disk through Unified Login, once
verified by test, removes the signed-URL step for Reels.

## 4. TikTok

### Adapters

Three, chosen per account (spec 03 §5):

| Adapter | Needs | Result | When to use |
| --- | --- | --- | --- |
| `tiktok_inbox` | Scope `video.upload` | Draft lands in the creator's TikTok inbox; the creator finishes and posts in the app. Target ends in `needs_user`, then `published` when TikTok reports it. | Default until the audit passes, if test T-2 shows it works for Runway's app status |
| `tiktok_direct` | Scope `video.publish` **and** `tiktok_app_status=audited` | Posted by the API | After the audit |
| `vendor:<name>` | A vendor account and the owner's approval to use it | Posted through the vendor's audited app | Bridge for public posting before the audit |
| `assisted` | Nothing | Manual checklist | Always available |

`tiktok_direct` must never be offered below `audited`: it would post `SELF_ONLY` and require the
creator to make their whole account private, which helps nobody.

### Connect

- TikTok needs an HTTPS redirect. Local installs use the public edge for the callback path only
  (spec 04 §6). Sandbox apps accept up to 10 owned accounts as target users.
- Store `expires_in` and `refresh_expires_in` from each response; refresh under the account
  lock; persist the **new** refresh token before releasing the lock.
- Handle `authorization.removed` webhooks: mark `needs_reauth`, stop jobs, start the deletion
  clock.

### Target settings (`tiktok_video`)

Rendered as TikTok's mandatory screen (research/tiktok §7), driven by a fresh `creator_info`
call each time the screen opens:

| Field | Rule |
| --- | --- |
| `title` | Caption, ≤ 2,200 UTF-16 code units, editable |
| `privacy_level` | Required; options only from `privacy_level_options`; **no preselection** |
| `allow_comment`, `allow_duet`, `allow_stitch` | All start off; disabled and greyed when `creator_info` says so |
| `commercial_content` | Off by default; when on, at least one of `your_brand` / `branded_content`; branded content disables private visibility |
| `is_aigc` | Defaults from the post's `ai_generated` |
| `cover_timestamp_ms` | Optional |
| consent | The exact declaration text for the chosen commercial options, shown above the release button |

These choices are part of the payload hash. They can only be made by a human session, never by
a key (spec 06 §4).

`tiktok_draft` (inbox) has no post settings: the creator sets everything inside TikTok. The
release dialog says "This sends a draft to your TikTok inbox. You finish and post it in the
app."

### Send (`tiktok_direct`)

1. Preflight: `creator_info` again; the chosen privacy still offered; duration within
   `max_video_post_duration_sec`; posting allowed.
2. Mark `request_sent_at`; `POST /v2/post/publish/video/init/` with `source=FILE_UPLOAD`,
   `video_size`, `chunk_size` (10 MB; whole file if under 5 MB), `total_chunk_count` =
   floor(size ÷ chunk). Init is the point of no return because TikTok publishes as soon as the
   upload completes.
3. `PUT` chunks in order with `Content-Range`. `206` continue, `201` done. Finish within the
   hour the URL is valid.
4. State `processing`. Wait for `post.publish.complete` or poll status (≤ 30 per minute).
5. `PUBLISH_COMPLETE`: `published`. `publicaly_available_post_id` present or
   `post.publish.publicly_available` received: `verified`, with the post id.
6. `FAILED`: map `fail_reason`. Retryable: `internal`, `video_pull_failed`. Final with a clear
   message: format, duration, frame-rate and size checks; `spam_risk*`; `auth_removed`.

Send (`tiktok_inbox`): the same with the inbox endpoint; `SEND_TO_USER_INBOX` moves the target
to `needs_user`; `PUBLISH_COMPLETE` (creator posted) moves it to `published`. Respect the cap of
5 pending shares per 24 hours.

Reconcile: status fetch by `publish_id`. If init never returned a `publish_id`, the post does
not exist: `failed_retryable`.

## 5. Capability matrix changes

`capabilities.py` moves from a static matrix per platform to a function of
`(platform, account, app_status, profile)`. Publishing states become:

| State | Meaning |
| --- | --- |
| `ready` | Adapter implemented, scope granted, app status sufficient |
| `ready_draft_only` | TikTok inbox |
| `needs_scope` | User must grant the publishing scope |
| `needs_media_host` | Instagram in a local install without R2 |
| `needs_app_approval` | Platform review not passed for this kind of account |
| `assisted_only` | Manual workflow |

`docs/PLATFORM_CAPABILITIES.md` is regenerated from this function so the document cannot drift
from the code.

## 6. Mock adapters

`RUNWAY_PLATFORM_PROVIDER=mock` gains mock publishers that walk the full state machine on a
timer, with switches to force each failure (`uncertain`, container `ERROR`, quota exceeded,
revoked token). The demo banner stays. All UI and worker tests run against these; no test
touches a real platform (invariant 9).
