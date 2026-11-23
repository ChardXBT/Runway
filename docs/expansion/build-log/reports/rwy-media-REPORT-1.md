Done: all six work packages are built, tested, and committed on `expansion/k-media`: the limits module, the storage port with `LocalDiskStore`, `R2Store` with its own SigV4 signer, the probe, validation, and the delivery helpers.

# rwy-media report

Branch `expansion/k-media`, base `f0c136f` (Merge expansion research and specs). Nothing was pushed or merged.

## Commits

| Hash | Subject |
| --- | --- |
| `a0cff9a` | feat(platforms): add dated platform media and publishing limits module |
| `b826736` | feat(mediastore): add storage port and resumable LocalDiskStore |
| `ee3e130` | feat(mediastore): add R2Store with a SigV4 signer on httpx |
| `d21bca5` | feat(mediastore): probe media with locked-down ffprobe and read MP4 layout |
| `d36350e` | feat(mediastore): validate probed media per platform post type |
| `0c8626a` | feat(mediastore): add YouTube, TikTok and Instagram delivery helpers |
| `4075f22` | feat(mediastore): expose LocalDiskStore.path_for for the probe job |

Each commit's `runway/mediastore/__init__.py` imports only modules that exist in that commit. I checked this for every commit.

## Files added (26 files, +6,217 lines, no existing file changed)

- `src/runway/platforms/limits.py`
- `src/runway/mediastore/`: `__init__.py`, `errors.py`, `keys.py`, `store.py`, `local.py`, `sigv4.py`, `r2.py`, `mp4atoms.py`, `probe.py`, `validation.py`, `delivery.py`
- `tests/unit/`: `test_platform_limits.py`, `test_mediastore_local.py`, `test_mediastore_sigv4.py`, `test_mediastore_r2.py`, `test_mediastore_probe.py`, `test_mediastore_validation.py`, `test_mediastore_delivery.py`
- `tests/unit/fixtures/ffprobe/*.json`: six small ffprobe outputs (H.264/AAC MP4, HEVC rotated MOV, VP9/Opus WebM, JPEG named `.mp4`, PNG, AAC-only M4A). I captured them from clips generated with `ffmpeg -f lavfi` and trimmed noise fields. No video is committed.
- `tests/integration/test_mediastore_ffmpeg.py`: tests against the real binaries, skipped when ffmpeg is not on PATH.

The `runway.media` package, `settings.py`, the database, the routes and the dependencies are unchanged.

## Migrations

None, as instructed.

## Tests run

| Run | Result | Duration |
| --- | --- | --- |
| New targeted tests (8 files) at `4075f22` | 278 passed | 11 s |
| Full backend suite (`pytest`, no path) at `0c8626a`, holding `heavy.lock` | **805 passed, 1 skipped, exit 0** | 1,904 s (31 min 44 s) |

Per file: limits 11, local store and keys 57, SigV4 11, R2 21, probe 32, validation 111, delivery 30, ffmpeg integration 5.

- The one skip and the one warning in the full run come from code I did not touch:
  - `test_cli_health.py::test_minimum_node_matches_the_installed_next_engines_field` is skipped by its own condition.
  - The warning is a Starlette `httpx` deprecation warning raised by `fastapi.testclient`.
- **Gotcha: the full run is on `0c8626a`, not the final commit.** The last commit `4075f22` adds only `LocalDiskStore.path_for` and its test, after the full run. After it I re-ran the eight targeted files, ruff and mypy, all clean. I did not repeat the full suite, because the rules allow one full run.
- **Gotcha: two earlier full-suite attempts produced no result.** The first stopped when my tool call hit its 30-minute limit, during the slow `test_youtube_publisher.py`. The second was a background job of my shell and stopped at the same point without writing an exit code. Both times I still held the lock and released nothing early. The counted run was a detached process that removed the lock in a `finally` block at 14:55:09.
- **Lock: a new `heavy.lock` appeared at 14:55:29, after I released mine.** It belongs to another job, so I left it alone.

## Lint and types

- `ruff check .`: all checks passed.
- `ruff format --check .`: 233 files already formatted.
- `mypy src/runway`: no issues in 138 source files.

## Public API of `runway.mediastore` (for the next job)

Everything below is importable from `runway.mediastore` unless a module is named.

### Keys (`keys.py`)

- `make_media_key(workspace_id: str | int, asset_id: str | int, sha256: str, extension: str) -> str`
  - Returns `w/{ws}/{asset}/{sha256[:16]}.{ext}`.
  - IDs must match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` and must not be Windows device names. The digest is 64 hex characters, lower-cased. The extension is 1–10 lowercase letters or digits.
- `parse_media_key(key: str) -> MediaKey`
  - `MediaKey` has `workspace_id`, `asset_id`, `digest_prefix`, `extension` and `.workspace_prefix`.
  - Raises `InvalidMediaKey` for anything else, including traversal, backslashes, drive letters, ADS `:` and a trailing newline.
- `workspace_prefix(workspace_id) -> "w/{id}/"`
- `safe_extension(filename: str | None) -> str`: a cosmetic extension from the client's filename, or `"bin"`.

### Storage port (`store.py`)

```python
StoreKind = Literal["local", "r2"]; UploadMode = Literal["api", "direct"]
DEFAULT_PART_URL_TTL_SECONDS = 3600

@dataclass(frozen=True) class PartReceipt: part_number: int; size: int; etag: str
@dataclass(frozen=True) class UploadPlan:
    store; mode; key; upload_id; size; sha256; content_type; part_size; created_at
    part_count (property); part_numbers() -> range; part_offset(n) -> int; part_length(n) -> int
    to_dict() -> dict  /  UploadPlan.from_dict(dict)      # JSON-safe, for media_uploads
@dataclass(frozen=True) class UploadProgress:
    parts: tuple[PartReceipt, ...]; missing_part_numbers: tuple[int, ...]
    received_ranges: tuple[tuple[int, int], ...]   # inclusive, merged
    bytes_received: int; is_complete (property)
@dataclass(frozen=True) class StaleUpload: key: str; upload_id: str; initiated_at: datetime | None

class MediaStore(Protocol):
    kind: StoreKind
    def begin_upload(self, key, size, content_type, *, sha256) -> UploadPlan
    def part_upload_urls(self, plan, part_numbers=None, *, ttl_seconds=3600) -> dict[int, str]  # direct only
    def receive_part(self, plan, part_number, data: BinaryIO | bytes) -> PartReceipt           # api only
    def upload_progress(self, plan) -> UploadProgress
    def complete_upload(self, plan, parts: Sequence[PartReceipt] = ()) -> None   # idempotent
    def abort_upload(self, plan) -> None
    def abort_stale_uploads(self, older_than: datetime) -> list[StaleUpload]
    def open(self, key) -> BinaryIO            # seekable; seek + read = ranged read
    def signed_get_url(self, key, ttl_seconds) -> str | None
    def delete(self, key) -> None              # missing object is not an error
```

### `LocalDiskStore`

```python
LocalDiskStore(root: Path, *, part_size=8 MiB, max_object_bytes=None, clock=utc_now)
LocalDiskStore.from_settings(settings, **options)   # root = <data_dir>/media/uploads
.root -> Path
.path_for(key) -> Path                              # completed object only; for ffprobe
```

- Plans are `mode="api"`.
- Completed objects live at `root/w/...`. Uploads in progress live at `root/.staging/{upload_id}.part` and `.json`.
- `complete_upload` checks every part, the total size and the SHA-256, then moves the file into place.
- `signed_get_url` always returns `None`.
- `part_upload_urls` raises `UnsupportedOperation`.

### `R2Store`

```python
R2Config(account_id, bucket, access_key_id, secret_access_key, endpoint=None, region="auto")
    .endpoint_url  # https://{account_id}.r2.cloudflarestorage.com unless overridden
R2Store(config, *, client: httpx.Client | None = None, part_size=16 MiB,
        max_object_bytes=None, clock=utc_now)
.object_size(key) -> int | None
.close()
```

- Plans are `mode="direct"`. The part size grows automatically, rounded to MiB, so a file never needs more than 10,000 parts.
- `receive_part` raises `UnsupportedOperation`.
- `signed_get_url(key, ttl)` accepts a lifetime from 1 s to 7 days and raises `ValueError` outside that range.
- `abort_stale_uploads` only aborts uploads under `w/` whose key parses as a media key.

### Errors

All storage errors subclass `MediaStoreError`:

- `InvalidMediaKey` and `InvalidUploadRequest`, which are also `ValueError`
- `UploadNotFound`, `ObjectNotFound`, `ObjectExists`, `IncompleteUpload`, `SizeMismatch`, `ChecksumMismatch`, `UnsupportedOperation`
- `StorageBackendError(detail, status=None, retryable=False)`

Error messages never contain credentials or signed URLs.

### Probe (`probe.py`, `mp4atoms.py`)

```python
MediaKind = Literal["video", "image", "audio", "unknown"]
@dataclass(frozen=True) class ProbeResult:
    kind; container; format_names; size_bytes; duration_ms; bitrate; video_codec
    width; height (display dimensions, rotation applied); rotation; fps
    audio_codec; audio_sample_rate; audio_channels; moov_at_front; has_edit_list
    aspect_ratio (property); to_dict() / ProbeResult.from_dict()   # for probe_json
MediaTools(ffprobe="ffprobe", ffmpeg="ffmpeg", timeout_seconds=60.0)
MediaProber(tools: MediaTools | None = None)
    .probe(path: Path) -> ProbeResult
    .extract_poster(source: Path, destination: Path (.jpg), *, at_ms=0, max_width=1280) -> Path
parse_ffprobe(payload: Mapping, *, layout: Mp4Layout | None = None, size_bytes: int | None = None) -> ProbeResult
default_poster_time_ms(probe) -> int
# runway.mediastore.mp4atoms: read_layout(stream) / read_layout_from_path(path) -> Mp4Layout | None
```

Probe errors are `ProbeError`, with subclasses `MediaToolMissing` (the message tells the user to install FFmpeg or configure the path) and `ProbeTimeout`.

### Validation (`validation.py`)

```python
@dataclass(frozen=True) class Issue:
    severity: Literal["error", "warning"]; code: str; message: str
    fix: str | None = None; source: str | None = None; item: int | None = None
validate(probe, platform: Platform | str, post_type: PostType, *, creator_max_duration_s: int | None = None) -> list[Issue]
validate_carousel(children: Sequence[ProbeResult]) -> list[Issue]
```

- `PostType` lives in `runway.platforms.limits`: `youtube_video`, `youtube_short`, `instagram_reel`, `instagram_image`, `instagram_carousel` (one child), `instagram_story`, `tiktok_video`.
- Issue codes are stable strings: `media_kind_not_supported`, `file_too_large`, `duration_too_short`, `duration_too_long`, `duration_exceeds_creator_limit`, `container_not_supported`, `moov_not_at_front`, `edit_list_present`, `video_codec_not_supported`, `audio_codec_not_supported`, `audio_sample_rate_too_high`, `audio_channels_too_many`, `frame_rate_out_of_range`, `width_too_large`, `dimensions_out_of_range`, `aspect_ratio_out_of_range`, `aspect_ratio_not_recommended`, `bitrate_above_recommended`, `image_format_not_supported`, `image_width_resized`, `short_not_vertical`, `short_too_long`, `carousel_item_count`, `carousel_image_cropped`, `probe_incomplete`.

### Delivery (`delivery.py`)

```python
ByteRange(first, last, total): length, is_last, content_range ("bytes a-b/total")
Chunk(range: ByteRange, data: bytes)
read_range(stream, byte_range) -> bytes                  # EOFError if the file is short
youtube_chunk_ranges(total_size, *, start_offset=0, chunk_size=8 MiB) -> list[ByteRange]
iter_youtube_chunks(stream, total_size, *, start_offset=0, chunk_size=8 MiB) -> Iterator[Chunk]
youtube_resume_offset(range_header: str | None) -> int  # "bytes=0-N" -> N+1, None -> 0
youtube_status_query_headers(total_size) -> {"Content-Length": "0", "Content-Range": "bytes */N"}
plan_tiktok_chunks(video_size, chunk_size=10_000_000) -> TikTokChunkPlan
TikTokChunkPlan(video_size, chunk_size, total_chunk_count, chunks).source_info() -> dict
iter_tiktok_chunks(stream, plan) -> Iterator[Chunk]
instagram_poll_schedule(url_ttl_seconds=7200) -> tuple[int, ...]  # 60..300, then every 300 to 7200
```

### Limits (`runway.platforms.limits`)

- Each limit is a frozen dataclass with `source`, `read_on` and `label`. `label` is one of `official`, `community`, `unverified`, `inference` or `decision`.
- Lookups: `VIDEO_LIMITS[post_type]`, `IMAGE_LIMITS[post_type]`, `POST_TYPE_PLATFORM`.
- Named values: `YOUTUBE_SHORTS_HEURISTIC`, `INSTAGRAM_CAROUSEL`, `YOUTUBE_TITLE`, `YOUTUBE_DESCRIPTION`, `INSTAGRAM_CAPTION`, `INSTAGRAM_ALT_TEXT`, `INSTAGRAM_COLLABORATORS`, `TIKTOK_TITLE`, `YOUTUBE_UPLOAD`, `TIKTOK_CHUNKS`, `INSTAGRAM_POLLING`, `DAILY_PUBLISH_LIMITS` (plus each `RateLimit` by name), `TIKTOK_INIT_RATE`, `TIKTOK_STATUS_FETCH_RATE`, `TIKTOK_PHOTO` (deferred), `R2_STORAGE`, `VALIDATION_HEURISTICS`.
- Unit constants: `KB`, `MB`, `GB` (decimal) and `KiB`, `MiB`, `GiB` (binary).

## Deviations and decisions

1. **`begin_upload` takes `sha256=`.** The key contains `sha256[:16]`, and the protocol passes the key to `begin_upload`. So the client must send the full hash with `POST /api/v1/media`, not only at complete. Spec 02 §4 step 3 is ambiguous on this.
   - The store checks that the key's prefix matches the hash.
   - Rejected alternative: upload to a staging key and copy at complete. On R2 that costs a server-side copy of up to 4 GB.
2. **`complete_upload` takes the plan instead of the key.** The plan carries the upload id, size and hash that completion needs. I also added these protocol methods:
   - `part_upload_urls`, `receive_part`, `upload_progress`, `abort_upload`, `abort_stale_uploads`
   - Each store refuses the other mode's method with `UnsupportedOperation`. This keeps the API typed against `MediaStore` alone.
3. **Each store is the record of which parts arrived.** The local store uses a sidecar JSON; R2 uses `ListParts`. `media_uploads.parts_json` can be a cache or stay empty.
   - Local parts are written at their offset, fsynced, and only then recorded. A rewrite forgets the part first.
   - Concurrent sends of the same part are refused.
4. **R2 SHA-256 is not recomputed**, following spec 02 §4, which says to trust the store's checksums. Sizes are enforced three ways instead:
   - Each part URL signs `content-length`.
   - `complete_upload` checks every part size through `ListParts`.
   - The object's length is checked after completion; on a mismatch the object is deleted.
   - It is [Unverified] whether R2 itself enforces a signed `content-length` on a presigned PUT. The completion checks catch a mismatch either way.
   - Recommendation: the probe job hashes the bytes when it copies the R2 object to a temp file for ffprobe.
5. **Edit lists count only when they change the timeline.** That means more than one entry, an empty edit (`media_time = -1`), or a rate other than 1.0.
   - A single shift entry is ignored. ffmpeg writes one for AAC priming and B-frame delay, and so do phones.
   - Counting every `elst` would have blocked nearly every file from Instagram, including ffmpeg's own output.
   - The fix named for `edit_list_present` is `-use_editlist 0` together with `+faststart`.
6. **Units.** Maximum sizes use decimal MB/GB (the stricter reading). The TikTok chunk bounds hold under both readings: minimum 5 MiB, maximum 64,000,000 bytes, final chunk at most 128,000,000 bytes. The default chunk is 10,000,000 bytes, as in TikTok's example.
   - A file no larger than the chunk size goes as one chunk, because `floor(size / chunk)` would be 0.
7. **Inferred rules, labelled `inference` in the limits module.**
   - Instagram carousel video children use the Reel rules.
   - Story images use JPEG and 8 MB and are warned outside 9:16, instead of being held to the feed 4:5–1.91 range.
8. **Severities beyond the spec table.**
   - Errors: the hard Reel and Story aspect bounds from research (0.01–10 and 0.1–10).
   - Warnings: Instagram image width outside 320–1440 px (Instagram resizes), bitrate above 25 Mbps, Shorts orientation and length, and `probe_incomplete` when the probe could not read a value a rule needs.
9. **WebM versus Matroska.** ffprobe reports both as `matroska,webm`. A file counts as WebM only if it has VP8, VP9 or AV1 video and Opus or Vorbis audio (or none); anything else is `matroska`. MOV versus MP4 comes from the `ftyp` major brand (`qt  ` means MOV).
10. **I did not change settings.** The ffmpeg paths come from `MediaTools`, and R2 credentials come from `R2Config`. This avoids merge conflicts with the job working in parallel.
11. **No real network.**
    - Every R2 test uses an `httpx.MockTransport` S3 fake that recomputes SigV4 for what httpx actually sent, so a wrong secret gets 403 (tested).
    - I did not fetch the AWS or Cloudflare pages. The SigV4 expected values are the documented ones from the AWS pages named in the test docstring.
    - Five signatures and one canonical-request hash match. A misremembered value could not match by chance.
    - The R2 multipart part limits (5 MiB to 5 GiB, 10,000 parts) are therefore labelled `unverified`.
12. **Stale-upload cleanup scope.** `LocalDiskStore.abort_stale_uploads` removes staged uploads that have a state file. It does not remove a stray `.part` file without one.
13. **Process safety.** The local store's locks are per process (`threading`). Running several API worker processes on one data dir would need a file lock.

## Owner questions

1. Does Instagram accept Reels whose only edit list is a single start shift? I decided "yes" (decision 5). If Meta rejects them, make `has_edit_list` count any `elst` entry, in `mp4atoms._edit_list_changes_timeline`.
2. Should a missing probe value (`probe_incomplete`) block Instagram, or stay a warning as now?
3. Is the default R2 part size of 16 MiB right for your upload speeds? Smaller parts resume faster; larger parts mean fewer requests.

## What the next job must know

- **Wiring.**
  - Use `LocalDiskStore.from_settings(settings)` in the local profile.
  - Use `R2Store(R2Config(...))` when R2 credentials are configured. Add the settings fields (R2 account, bucket, keys; ffprobe and ffmpeg paths); none exist yet.
  - Persist `UploadPlan.to_dict()` in `media_uploads`, and `ProbeResult.to_dict()` in `media_assets_v2.probe_json`.
- **Routes.**
  - `POST /media`: `make_media_key(ws, asset, sha256, safe_extension(filename))`, then `begin_upload`. In `direct` mode, return `part_upload_urls(plan)` with `part_size`.
  - `GET /media/{id}/upload`: `upload_progress(plan)`. In direct mode, add fresh `part_upload_urls(plan, progress.missing_part_numbers)`.
  - `PUT` part (api mode): `receive_part(plan, n, stream_or_bytes)`. It reads at most part length + 1 bytes, so pass a synchronous file-like object over the request body.
  - `POST /media/{id}/complete`: `complete_upload(plan)`. On `ChecksumMismatch` or `SizeMismatch`, call `abort_upload` and mark the asset `rejected`.
- **Workspace isolation.** Always build keys from the authenticated workspace id. Never accept a key from the client. `parse_media_key(key).workspace_id` lets a route assert ownership.
- **Probe job.**
  - Local: `prober.probe(store.path_for(key))`.
  - R2: copy `store.open(key)` to a server-generated temp path and probe that.
  - Set the asset kind from `probe.kind`: `video` or `image` becomes ready; `audio` or `unknown` becomes rejected.
  - Poster: `extract_poster(path, poster_path, at_ms=default_poster_time_ms(probe))`.
- **Nightly job.** `store.abort_stale_uploads(now - timedelta(days=...))` on both stores.
- **Instagram from a local install.** `LocalDiskStore.signed_get_url` returns `None`, which should surface as `needs_media_host`. A copy to R2 at publish time has no helper yet. It would need a server-side multipart upload: `begin_upload` + signed `PUT` of each part + `complete_upload`.
- **Publishers.**
  - YouTube: `iter_youtube_chunks(store.open(key), size, start_offset=youtube_resume_offset(range_header))`.
  - TikTok: `plan_tiktok_chunks(size).source_info()` in the init body, then `iter_tiktok_chunks`.
  - Instagram: poll on `instagram_poll_schedule()`.
- **R2 CORS.** Browser uploads need the bucket's CORS to allow `PUT` from the web origin. Exposing `ETag` is not needed, because completion reads the parts from R2.
