# Spec 02 — Media pipeline

Decision D3. Research: platform files §"Media", and
[`../research/hosting-and-sync.md`](../research/hosting-and-sync.md) §1.

Today drafts cannot carry media at all (audit C1). This spec adds upload, storage, validation
and delivery to platforms.

## 1. Requirements

- Accept video up to at least 4 GB (TikTok's limit; YouTube allows far more) and images.
- Survive a dropped connection during upload.
- Tell the user, before scheduling, which targets a file is valid for and why not.
- Deliver bytes to YouTube and TikTok directly; give Instagram a URL it can fetch.
- Work with no cloud account in the `local` profile for YouTube and TikTok.
- Never expose one workspace's media to another.

## 2. Storage port

```python
class MediaStore(Protocol):
    def begin_upload(self, key: str, size: int, content_type: str) -> UploadPlan: ...
    def complete_upload(self, key: str, parts: list[PartReceipt]) -> None: ...
    def open(self, key: str) -> BinaryIO: ...              # ranged reads for platform uploads
    def signed_get_url(self, key: str, ttl_seconds: int) -> str | None: ...
    def delete(self, key: str) -> None: ...
```

Two implementations:

| | `LocalDiskStore` (`local` default) | `R2Store` (`hosted`; optional in `local`) |
| --- | --- | --- |
| Upload | Browser sends chunks to the API, which appends to a file under `data/media/uploads/` | Browser uploads parts straight to R2 using signed part URLs (S3 multipart); the API never carries the bytes |
| `open` | file handle | ranged `GET` |
| `signed_get_url` | `None` unless a public edge is configured | signed URL on the R2 S3 domain |

Keys are `w/{workspace_id}/{asset_id}/{sha256[:16]}.{ext}`. The workspace prefix makes a
cross-workspace mistake visible and lets a workspace be purged by prefix.

Why R2: 10 GB free, no egress fee (Instagram and TikTok pulling a 300 MB file costs nothing),
S3-compatible so the choice is reversible, and the owner already uses Cloudflare. Rejected:
serving media from the app host (bandwidth, and a public file server on the API is a security
surface); Supabase or S3 (egress fees or a new vendor).

Why direct browser-to-storage upload in hosted mode: a 4 GB upload through a small API machine
ties up memory and a worker for minutes and doubles bandwidth.

Constraint to respect: R2 signed URLs work only on the S3 API domain, not on a custom domain,
and last at most 7 days. So:

- Instagram gets a signed URL on the S3 domain with a 2-hour lifetime, created at publish time.
- TikTok `PULL_FROM_URL` is **not used**: it needs a verified domain, which signed S3-domain
  URLs cannot satisfy. TikTok video goes by `FILE_UPLOAD`. TikTok photo posts are deferred.

## 3. Data model

```
media_assets_v2(                      -- name avoids the existing intelligence table media_assets
  id, workspace_id, kind ['video'|'image'], original_filename, content_type,
  size_bytes, sha256, storage ['local'|'r2'], storage_key,
  status ['uploading'|'processing'|'ready'|'rejected'|'deleted'],
  probe_json,            -- container, codecs, duration_ms, width, height, fps, bitrate,
                         -- audio codec/rate/channels, moov_at_front, has_edit_list
  poster_key,            -- extracted frame for previews
  created_by, created_at, ready_at, delete_after )
media_uploads(id, workspace_id, asset_id, upload_id, part_size, parts_json, expires_at)
```

`posts.media` references assets through a join table with an order column (spec 03).

## 4. Upload flow

1. `POST /api/v1/media` with filename, size, content type. Server checks quota and size, creates
   the asset in `uploading`, returns an upload plan: either `{mode: "api", chunk_size}` or
   `{mode: "direct", part_urls[], part_size}`.
2. Client uploads parts. On failure it asks `GET /api/v1/media/{id}/upload` for what has arrived
   and resumes.
3. `POST /api/v1/media/{id}/complete`. Server verifies size and SHA-256 (client sends the hash;
   server recomputes for local files and trusts the store's checksum for R2 parts), sets
   `processing`, and enqueues a probe job.
4. Probe job runs `ffprobe` (JSON output), extracts a poster frame, writes `probe_json`, sets
   `ready` or `rejected` with a reason.

Security rules:

- Decide the type from the probe, not from the filename or the client's content type.
- Enforce size at the storage layer (signed part URLs carry a length), not only in the API.
- Run `ffprobe` with a timeout, no network protocols enabled, on a file path the server
  generated. Never pass a user-supplied string as an argument.
- Uploaded media is never served from the API origin in hosted mode. Previews use short-lived
  signed URLs.

## 5. Validation per target

A pure function `validate(asset.probe, platform, post_type) -> list[Issue]` with the limits in
one data module, each limit carrying its source and the date read.

| Check | YouTube video | Instagram Reel | Instagram Story video | TikTok video |
| --- | --- | --- | --- | --- |
| Max size | 256 GB | 300 MB | 100 MB | 4 GB |
| Duration | (channel-dependent; do not block) | 3 s – 15 min | 3 s – 60 s | ≤ creator's `max_video_post_duration_sec`, ≤ 10 min |
| Container | any `video/*` | MP4 or MOV, moov at front, no edit lists | same | MP4, WebM or MOV |
| Video codec | — | H.264 or HEVC | same | H.264, H.265, VP8, VP9 |
| Audio | — | AAC ≤ 48 kHz, ≤ 2 channels | same | — |
| Frame rate | — | 23–60 | same | 23–60 |
| Dimensions | — | width ≤ 1920 | same | each side 360–4096 |
| Aspect | warn if not vertical for a Short | warn outside 9:16 | warn outside 9:16 | — |

Images for Instagram: JPEG only, ≤ 8 MB, aspect 4:5 to 1.91:1.

Issues are `error` (blocks release for that target) or `warning` (shown, does not block). The
post editor shows them per target as soon as the probe finishes. A file can be valid for
YouTube and TikTok and invalid for Instagram; the other targets proceed.

Not in phase 1: transcoding. If Instagram rejects for `moov` placement or edit lists, the
message says exactly that and names the fix (`ffmpeg -movflags +faststart`). A "fix it for me"
button is a later feature. Reason: transcoding needs CPU time and disk that the free and
cheapest hosting tiers do not have, and silent re-encoding changes the creator's file.

## 6. Delivery to platforms

- **YouTube:** stream from `MediaStore.open()` into the resumable session in 8 MB chunks
  (a multiple of 256 KB). Persist the session URI and confirmed offset on the attempt so a
  restarted worker resumes instead of re-uploading.
- **TikTok:** `FILE_UPLOAD` in 10 MB chunks (within 5–64 MB), sequentially; the upload URL is
  valid for one hour, so a stalled upload must restart from `init`.
- **Instagram:** create the container with `video_url` or `image_url` set to
  `signed_get_url(key, 7200)`; poll status once a minute for at most 5 minutes, then every 5
  minutes up to the URL's lifetime for large Reels [Inference: the documented 5 minutes is a
  recommendation for typical files].

`local` profile and Instagram: `LocalDiskStore.signed_get_url` returns `None`, so Instagram
publishing reports "needs a public media location" with two ways to enable it: configure R2
credentials (the asset is copied to R2 at publish time and deleted after), or stay on the manual
workflow. This is honest about the one thing a loopback-only install cannot do.

## 7. Retention and deletion

- `delete_after` defaults to 30 days after the last target of the last post using the asset
  reaches a terminal state. The workspace can keep assets longer within its quota.
- R2 copies made only for an Instagram fetch are deleted when the container reaches `FINISHED`,
  `ERROR` or `EXPIRED`.
- A nightly job deletes expired assets and abandoned multipart uploads.
- Deleting a workspace deletes the `w/{id}/` prefix.

Quota: per-workspace storage limit, default 5 GB in hosted mode [Inference: half the R2 free
tier for the first workspaces; tune when there is real use].

## 8. Acceptance criteria

- A 2 GB file uploads in both store modes, survives one forced disconnect, and resumes without
  re-sending completed parts.
- A file with a wrong extension is typed by its contents.
- A Reel-invalid file shows an Instagram error and still schedules for YouTube.
- A workspace cannot fetch another workspace's asset by id or by guessing a key.
- No media bytes pass through the API process in `R2Store` direct mode.
