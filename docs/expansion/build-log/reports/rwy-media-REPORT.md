Done: all 11 review findings are fixed on `expansion/k-media`, each with a test that fails on the old code, and the six original work packages are still built and tested.

# rwy-media report (after review fixes)

Branch `expansion/k-media`, base `f0c136f`. Nothing was pushed or merged.

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
| `dd68642` | fix(mediastore): split large TikTok files, list YouTube containers, warn on unknown edit lists |
| `35a888e` | fix(mediastore): bind keys to workspaces, verify R2 hashes, lock uploads across processes |
| `e9c3514` | fix(mediastore): whitelist demuxers, probe only server-named inputs, scrub paths from errors |

I exported each of the two intermediate fix commits to a temporary directory and ran all eight mediastore and limits test files against it: `dd68642` 295 passed, `35a888e` 326 passed.

## Files

- Added in the review fixes: `src/runway/mediastore/filelock.py`.
- Changed in the review fixes: `limits.py` and the mediastore modules `__init__`, `delivery`, `errors`, `keys`, `local`, `mp4atoms`, `probe`, `r2`, `store`, `validation`, plus the eight test files. That is 19 files, +1,653/−390 since `4075f22`.
- No existing Runway file outside `runway.mediastore`, `runway.platforms.limits` and their tests is changed.
- `runway.media`, settings, the database, routes and dependencies are untouched.

## Migrations

None.

## Tests run (after the review fixes, at `e9c3514`)

| Run | Result | Duration |
| --- | --- | --- |
| Mediastore and limits tests (8 files, including the real-ffmpeg integration file) | 337 passed | 18 s |
| Fast unit set `pytest tests/unit` | 671 passed, 1 skipped | 266 s |

- Per file: limits 13, local store and keys 77, SigV4 11, R2 32, probe and MP4 layout 39, validation 116, delivery 40, ffmpeg integration 9.
- The skip is `test_cli_health.py:74` ("apps/web dependencies are not installed"), which is not my code.
- As instructed, I did not run the full suite again. The last full run (805 passed, 1 skipped) was at `0c8626a`, before the review.
- Ruff and mypy:
  - `ruff check .`: all checks passed.
  - `ruff format --check .`: 234 files already formatted.
  - `mypy src/runway`: no issues in 139 files.
  - A throwaway strict-mypy check that assigns `LocalDiskStore` and `R2Store` to `MediaStore` also passed, so both stores satisfy the protocol.

## Review fixes

**How I showed each test fails before the fix.** Most new tests need the new API (`workspace_id`, `new_probe_input`, `copy_to_path`), so they cannot even import on the old code. Instead I exported `4075f22` to a temp directory, put it first on `PYTHONPATH`, and ran a throwaway script reproducing each scenario on the old code. That script is deleted. "Before" below is what it printed.

### 1. HLS and concat playlists read another workspace's file (security)

- **Before:** `probe(upload.m3u8)` returned the victim video's metadata (320x240).
- **Poster extraction:** on the old code this happened to fail for this HLS file ("no packets"), but nothing prevented it.
- **Fix:**
  - ffprobe and ffmpeg both get `-format_whitelist` placed before the input. The list is `probe.ALLOWED_DEMUXERS`: `3g2,3gp,jpeg_pipe,m4a,matroska,mj2,mov,mp4,webm`.
  - That list is derived from the containers the limits module accepts. Importing the probe raises if a post type lists no containers.
  - The probe and poster extractor only accept server-named `{random}.input` files (`new_probe_input`). The probe job fills one with `store.copy_to_path`.
  - Key extensions are now chosen by the server from the content type (`key_extension`), never taken from the filename. `safe_extension` is removed.
  - YouTube gets the explicit container list `{mp4, mov, webm}`.
  - The poster command also gets `-update 1`, so the output name is never read as a `%d` image-sequence pattern.
- **Tests:**
  - Real ffmpeg: `test_mediastore_ffmpeg.py::test_playlists_pointing_at_other_files_are_refused`, with three cases: an HLS `file:` URL, an HLS bare path, and an `ffconcat` list. Each is refused by `probe` and by `extract_poster`, and no poster is written. The files are also refused under the client names `.m3u8` and `.ffconcat`.
  - Unit: `test_allowed_demuxers_are_exactly_what_validation_accepts`, `test_only_server_named_inputs_are_accepted`, `test_probe_and_poster_arguments_are_locked_down`.
  - Keys: `test_key_extension_is_chosen_by_the_server`, and an `m3u8` row in `test_make_media_key_rejects_unsafe_components`.
  - Validation rows: YouTube `matroska`, `avi` and `hls` are now errors.

### 2. Probe errors exposed absolute server paths

- **Before:** "ffprobe could not read the file: file:C:\Users\…\junk.mp4: Invalid data found…".
- **Fix:**
  - `safe_tool_message()` keeps the last stderr line and replaces the known input and output paths (in plain, `file:` and POSIX forms) with `<file>`.
  - As a backstop it also replaces any remaining Windows, UNC or POSIX absolute path, and removes `@ 0x…` addresses.
  - `MediaToolMissing` shows only the binary's name.
  - A failed poster run deletes any partial output.
- **Tests:**
  - `test_probe_timeout_and_failure_messages_carry_no_paths` replaces the old test that asserted on a message containing a path.
  - `test_safe_tool_message_removes_paths`, five cases.
  - `test_missing_binary_gives_a_clear_error_without_paths`.
  - Real ffmpeg: `test_unreadable_file_raises_probe_error_without_paths`.

### 3. `R2Store` never verified SHA-256

- **Before:** no verification existed.
- **Fix:**
  - `copy_to_path(key, destination, *, workspace_id, sha256, size)` on both stores. It writes the object to a server path and checks size and SHA-256 as the bytes arrive.
  - On R2 it is one streaming GET. Locally it makes a hard link, or a copy when the destination is on another volume.
  - On a mismatch it raises `ChecksumMismatch` or `SizeMismatch` and leaves no file. It stops reading as soon as the size is exceeded.
  - The probe job must call it; the docstring for `complete_upload` says so.
  - I chose this over per-part `x-amz-checksum-sha256`. Whether R2 supports that header on presigned `UploadPart` is unverified, and it would require the browser to hash every part first.
- **Tests:**
  - `test_mediastore_r2.py::test_copy_to_path_rejects_bytes_that_do_not_match_the_declared_hash`: the client declares one hash, uploads other bytes of the same size, and completion succeeds. `copy_to_path` then raises `ChecksumMismatch`; it raises `SizeMismatch` for extra bytes and `ObjectNotFound` for a missing key.
  - `test_copy_to_path_verifies_sha256_of_the_stored_bytes`.
  - Local: `test_copy_to_path_links_and_verifies` and `test_copy_to_path_falls_back_to_a_verified_copy`.

### 4. Keys were not bound to a workspace

- **Before:** `open` had no workspace argument, and `make_media_key("WS", …)` was accepted, so `w/WS/` could alias `w/ws/`.
- **Fix:**
  - These methods now take a required keyword `workspace_id` and raise `WorkspaceMismatch` (a subclass of `InvalidMediaKey`) before any I/O or request: `begin_upload`, `open`, `copy_to_path`, `signed_get_url`, `signed_preview_url`, `delete`, and `R2Store.object_size`.
  - Ids are lowercase only (`[a-z0-9][a-z0-9_-]{0,63}`). Uppercase is rejected rather than lowercased, because lowercasing could itself merge two different ids.
  - The `keys.py` docstring is updated to match.
- **Tests:**
  - `test_mediastore_local.py::test_keys_are_bound_to_the_callers_workspace`.
  - `test_mediastore_r2.py::test_keys_are_bound_to_the_callers_workspace`, which also asserts that no request is sent.
  - Uppercase rows in `test_make_media_key_rejects_unsafe_components`, and `w/WS1/…` in the escaping-keys table.

### 5. An exhausted box budget reported `False` instead of unknown

- **Before:** ftyp + moov (2-entry elst) + mdat + 20,000 `free` boxes gave `has_edit_list=False`.
- **Fix:**
  - Each walk (top level, inside moov) has its own budget and an `incomplete` flag. The flag is set when the budget runs out or a box is malformed.
  - An answer the walk did not reach is `None`.
  - Validation emits a `probe_incomplete` warning when the edit-list state is `None` for Instagram.
- **Tests:**
  - `test_exhausted_top_level_budget_still_reads_the_moov` (the reviewer's file, which now gives `True`, the correct answer).
  - `test_exhausted_budget_inside_moov_is_unknown_not_false`, `test_moov_beyond_an_exhausted_top_level_walk_is_unknown`, `test_malformed_box_inside_moov_is_unknown`.
  - Validation rows `edit list … has_edit_list None` (warning for Instagram, nothing for TikTok).

### 6. Single-entry edit lists were ignored without a source

- **Before:** `has_edit_list=False` for one start-shift entry.
- **Fix:**
  - I found no citable source for the exemption, and the rules forbid fetching Meta's pages. So any `elst` with entries now counts.
  - The choice and its source are recorded as `limits.INSTAGRAM_EDIT_LIST_RULE` (label `decision`).
  - The error message explains that most encoders write one. The fix it names is `ffmpeg -i input.mp4 -c copy -use_editlist 0 -movflags +faststart output.mp4`.
  - **Consequence:** ffmpeg's default MP4 output now fails Instagram validation until re-muxed. The real-ffmpeg test confirms the named fix clears both errors.
- **Tests:**
  - `test_any_edit_list_entry_counts`.
  - `test_edit_list_rule_and_tiktok_split_rule_are_recorded_with_sources`.
  - Real ffmpeg: `test_layout_problems_are_detected_and_the_named_fixes_work`.

### 7. `LocalDiskStore` locks lived on the instance

- **Before:** two store instances received the same part at the same time.
- **Fix:**
  - New `filelock.py` uses the same mechanism as `TokenStore` (`msvcrt.locking` / `fcntl.flock`), with one handle per acquisition.
  - The upload lock `{id}.lock` guards every state read-modify-write.
  - The part lock `{id}.p{n}.lock` is taken without waiting while a part is written; if it is held, the second sender is refused.
  - State files are written to a unique temp name (`{id}.{random}.json.tmp`) and moved into place with `replace_with_retry`, which retries `PermissionError` 6 times with growing sleeps (about 1 s in total).
  - The in-memory locks are removed.
- **Tests:**
  - `test_same_part_cannot_be_received_twice_at_once[True]` (a second instance).
  - `test_a_part_held_by_another_process_is_refused` (a real subprocess holds the lock).
  - `test_parallel_parts_from_separate_instances_are_all_recorded`: six instances, with a delay injected into the read-modify-write.
  - `test_state_replace_retries_a_transient_permission_error`, covering two transient failures and then a permanent one with no temp file left.

### 8. Stale cleanup used the start time

- **Before:** an upload that received a part after it started was aborted, and orphan `.part` files were left behind.
- **Fix:**
  - The state records `updated_at` (from the store clock) on every save, and cleanup compares that.
  - Files of uploads that have no state file (`.part`, lock files) and leftover `*.tmp` files are removed once their mtime is before the cutoff.
  - Lock files that are still open on Windows are skipped and retried on the next run.
- **Tests:** `test_stale_cleanup_uses_last_activity_not_start_time`, `test_stale_cleanup_removes_orphans_and_temporary_files`, `test_state_records_last_activity`.

### 9. The client's Content-Type was stored

- **Before:** `text/html` was accepted.
- **Fix:**
  - `normalize_content_type` accepts only `video/*` and `image/*`, and rejects `+xml` subtypes such as `image/svg+xml`. This applies to both stores.
  - New `signed_preview_url(key, ttl, *, workspace_id, content_type)` adds `response-content-type` (validated the same way) and `response-content-disposition=attachment`.
  - `signed_get_url` stays plain for the Instagram fetch.
  - `probe.content_type_for(probe)` gives the probed MIME type.
- **Tests:**
  - `test_begin_accepts_only_video_and_image_content_types` (local, six types; R2, four).
  - `test_preview_url_forces_probed_type_and_attachment_but_fetch_url_stays_plain`.

### 10. The TikTok planner sent one 100 MB chunk

- **Before:** `plan_tiktok_chunks(100 MB, 64 MB)` gave one chunk of 100,000,000 bytes.
- **Source re-read:** `research/recheck-20261122.md` §3 says each chunk is 5–64 MB except the last (≤ 128 MB), with `floor(size / chunk)` chunks. The file is only on branch `expansion/build`; I read it with `git show 0f58898:…` without touching another worktree.
- **Fix:**
  - A file is a single chunk only if it is at most 64 MB (`TIKTOK_CHUNKS.max_single_chunk_bytes`).
  - Above that, `chunk_size = size // 2` gives two chunks of at most 64 MB each.
  - A non-final chunk above 64 MB raises.
  - The rule and its source are recorded in `limits.TIKTOK_CHUNKS`.
- **Test:** `test_tiktok_no_non_final_chunk_exceeds_64_mb_and_large_files_are_split`. Its 10 boundary cases are 5 MB, 64 MB, 64 MB + 1, 100 MB, 128 MB − 1, 128 MB, 128 MB + 1, 79 MB with 40 MB chunks, 19 MB with 10 MB chunks, and 65 MB with 10 MB chunks.

### 11. R2 ranged reads and the parallel-completion race

- **Ranged reads.** Before, a 200 response (Range ignored) was read whole into memory.
  - Fix: ranged reads use a streaming GET. On a 200 with offset 0, reading stops at the requested length. On a 200 with a later offset, it raises `StorageBackendError("R2 ignored the Range header…")` instead of downloading up to the offset.
  - Test: `test_ranged_read_stops_at_the_requested_length_when_range_is_ignored`. The fake sends 64 KiB blocks, and the test asserts exactly one block is read from a 4 MiB object.
- **Completion race.** A completion that gets 404 because a parallel completion finished now checks the object's size and returns normally. Otherwise it raises `UploadNotFound`.
  - Tests: `test_completion_finished_in_parallel_returns_normally` and `test_completion_404_without_an_object_is_upload_not_found`.

## Public API of `runway.mediastore` (current)

Everything is importable from `runway.mediastore`. Changes since the first report are marked **new** or **changed**.

### Keys

```python
make_media_key(workspace_id: str | int, asset_id: str | int, sha256: str, extension: str) -> str
    # ids: lowercase [a-z0-9][a-z0-9_-]{0,63}, no Windows device names        (changed)
    # extension must be in KEY_EXTENSIONS = {mp4, mov, webm, jpg, png, webp, bin}   (changed)
key_extension(content_type: str) -> str          # server-chosen; "bin" if unknown  (new)
parse_media_key(key) -> MediaKey                 # MediaKey(workspace_id, asset_id, digest_prefix, extension)
check_workspace(key, workspace_id) -> MediaKey   # raises WorkspaceMismatch            (new)
workspace_prefix(workspace_id) -> "w/{id}/"
# safe_extension(filename) was removed: the client's filename never chooses the extension.
```

### Storage port (`MediaStore` protocol; `LocalDiskStore` and `R2Store` both satisfy it)

```python
begin_upload(key, size, content_type, *, sha256, workspace_id) -> UploadPlan     (changed)
    # content_type: video/* or image/* only, no +xml
part_upload_urls(plan, part_numbers=None, *, ttl_seconds=3600) -> dict[int, str] # direct (R2)
receive_part(plan, part_number, data: BinaryIO | bytes) -> PartReceipt          # api (local)
upload_progress(plan) -> UploadProgress
complete_upload(plan, parts=()) -> None        # idempotent; local verifies SHA-256 here
abort_upload(plan) -> None
abort_stale_uploads(older_than: datetime) -> list[StaleUpload]  # by last activity (changed)
open(key, *, workspace_id) -> BinaryIO                                           (changed)
copy_to_path(key, destination: Path, *, workspace_id, sha256, size) -> Path      (new)
signed_get_url(key, ttl_seconds, *, workspace_id) -> str | None   # plain       (changed)
signed_preview_url(key, ttl_seconds, *, workspace_id, content_type) -> str | None (new)
delete(key, *, workspace_id) -> None                                             (changed)
```

- `LocalDiskStore(root, *, part_size=8 MiB, max_object_bytes=None, clock=…)`
  - Also has `.from_settings(settings)`, `.root`, and `.scratch_dir` (new: `root/.scratch`, on the same volume, so `copy_to_path` makes a hard link).
  - `path_for` was removed and replaced by `copy_to_path`.
  - Both signed-URL methods return `None`.
- `R2Store(R2Config(...), *, client=None, part_size=16 MiB, max_object_bytes=None, clock=…)`
  - Also has `.object_size(key, *, workspace_id)` and `.close()`.
- Value types are unchanged: `UploadPlan` (`to_dict` / `from_dict`), `PartReceipt`, `UploadProgress`, `StaleUpload`.
- Errors are as before, plus `WorkspaceMismatch(InvalidMediaKey)`.
  - A blocking lock that times out raises `runway.mediastore.filelock.LockUnavailable` after 30 s.

### Probe

```python
new_probe_input(directory: Path) -> Path      # {directory}/{random}.input              (new)
MediaProber(tools: MediaTools | None = None)
    .probe(path: Path) -> ProbeResult         # path must end in PROBE_INPUT_SUFFIX     (changed)
    .extract_poster(source: Path, destination: Path (.jpg), *, at_ms=0, max_width=1280) -> Path
MediaTools(ffprobe="ffprobe", ffmpeg="ffmpeg", timeout_seconds=60.0)
ALLOWED_DEMUXERS: tuple[str, ...]                                                       (new)
content_type_for(probe) -> str | None         # for signed_preview_url                (new)
safe_tool_message(text, paths=()) -> str                                                (new)
parse_ffprobe(payload, *, layout=None, size_bytes=None) -> ProbeResult
default_poster_time_ms(probe) -> int
```

### Validation and delivery

- `validate(probe, platform, post_type, *, creator_max_duration_s=None) -> list[Issue]` and `validate_carousel(children) -> list[Issue]`.
  - Issue codes are as before; `probe_incomplete` now also covers an unknown edit-list state.
- `plan_tiktok_chunks(video_size, chunk_size=10_000_000) -> TikTokChunkPlan` now splits files above 64 MB.
- The YouTube and Instagram helpers are unchanged.

### Limits (new entries)

- `YOUTUBE_CONTAINERS`
- `INSTAGRAM_EDIT_LIST_RULE` (`DetectionRule`)
- `TIKTOK_CHUNKS.max_single_chunk_bytes`
- `RECHECK_READ_ON`

## Deviations and decisions

Items from the first report that still hold:

- `begin_upload` takes the full SHA-256.
- `complete_upload` takes the plan.
- Each store is the record of which parts arrived.
- MB/GB are read as decimal for maximum sizes.
- Carousel and Story-image rules are inferred.
- WebM is told apart from Matroska by codec.
- No settings changes.
- No network calls.

The review fixes add these:

1. **Ids are lowercase only: uppercase is rejected, not lowercased.** Lowercasing would merge two ids that differ only by case. A caller with uppercase UUIDs must lowercase them itself.
2. **Edit lists (review item 6).** With no source for the exemption, any `elst` entry counts. ffmpeg's default MP4 output now fails Instagram validation until it is re-muxed with `-use_editlist 0`.
   - The issue message names the command. A real-ffmpeg test shows the command removes the elst without re-encoding.
3. **Demuxers.** PNG and WebP images are no longer probed: no post type accepts them, and they are outside the whitelist. They are rejected as "could not read the file as MP4, MOV, WebM or JPEG".
   - `image2` is deliberately not allowed. Server-named `.input` files make JPEG detection fall to `jpeg_pipe`, which a real-ffmpeg test confirms.
4. **SHA-256 on R2 is checked at probe time, not at completion** (see review item 3). The object sits in R2 unverified until the probe job runs. The asset is `processing` until then, so nothing can publish it.
5. **`copy_to_path` on the local store makes a hard link.**
   - The probe input shares bytes with the stored object, so the caller must treat it as read-only and delete it.
   - A probe link left behind by a crash costs no extra space until the object is deleted. `abort_stale_uploads` does not clean `.scratch`, because a link's mtime is the original file's.
6. **`signed_preview_url` relies on R2 honouring `response-content-type` and `response-content-disposition` on presigned GETs.** S3 does; for R2 this is [Unverified] working knowledge. If R2 ignores them, the stored type still applies, and it can only be a video or image type.
7. **TikTok:** a 64–128 MB file with the default 10 MB chunk size is planned in ordinary 10 MB chunks. The halving only applies when the chunk size is so large that `floor(size / chunk)` would be 1.
8. **Fixed fps assertion in a test.** A clip written without an edit list reports `avg_frame_rate` ≈ 29.8 instead of 30, so the real-ffmpeg test compares within 0.5 fps. Parsing is unchanged.

## Owner questions

1. Does Instagram really reject a Reel whose only edit list is ffmpeg's single start shift? If not, relax `INSTAGRAM_EDIT_LIST_RULE` and `mp4atoms._elst_has_entries` together.
2. Should `probe_incomplete` (including an unknown edit-list state) block Instagram, or stay a warning?
3. Should PNG and WebP be accepted anywhere (for example TikTok photos when they are built)? If so, add them to an `IMAGE_LIMITS` entry; the demuxer whitelist follows automatically.

## What the next job must know

- **Probe job (both stores):**
  ```python
  dest = new_probe_input(scratch)   # local: store.scratch_dir; R2: a server temp dir
  store.copy_to_path(key, dest, workspace_id=ws, sha256=asset.sha256, size=asset.size_bytes)
  try:
      probe = prober.probe(dest)
      poster = prober.extract_poster(dest, poster_tmp, at_ms=default_poster_time_ms(probe))
  finally:
      dest.unlink(missing_ok=True)
  ```
  - On `ChecksumMismatch` or `SizeMismatch`: mark the asset `rejected` and `store.delete(key, workspace_id=ws)`.
  - On `ProbeError`: mark it `rejected` with `str(exc)`, which is safe to store.
- **Routes:**
  - Build keys with `make_media_key(ws, asset_id, sha256, key_extension(content_type))`.
  - Always pass the authenticated workspace as `workspace_id`.
  - Previews: `signed_preview_url(key, ttl, workspace_id=ws, content_type=content_type_for(probe))`.
  - Instagram containers: `signed_get_url(key, 7200, workspace_id=ws)`.
- **`receive_part` for a part that is already arriving** raises `InvalidUploadRequest("… already being received")`. Map it to 409.
- **Nightly job:** call `abort_stale_uploads(now - retention)` on both stores. On the local store this also removes orphans.
- **R2 CORS** is unchanged: allow `PUT` from the web origin.
