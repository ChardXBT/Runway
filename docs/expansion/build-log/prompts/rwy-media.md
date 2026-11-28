Job name: **rwy-media**

Job rwy-media: build the media-pipeline core (phase P2.1 without database tables or routes, plus
the R2 store from P3.2) of the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-media`, branch
`expansion/k-media`. It already has a `.venv` with the project installed in editable mode.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read, in your worktree: `docs\expansion\specs\00-principles.md`,
`docs\expansion\specs\02-media-pipeline.md` (all), `docs\expansion\specs\08-platform-adapters.md`
(the media-related parts of §2-§4), `docs\expansion\research\youtube.md`,
`docs\expansion\research\instagram.md`, `docs\expansion\research\tiktok.md` (media sections), and
`src\runway\platforms\base.py`.

Another job is adding database tables, migrations, the job queue and the worker at the same time.
Do NOT add Alembic revisions, SQLAlchemy models or API routes. Build pure, well-tested modules that
the next job will wire to tables and routes. Put them in a new package `src\runway\mediastore\`
(the existing `runway\media` package belongs to the intelligence engine; do not change it).

## Work packages (all of them)

1. **Platform limits data module** `src\runway\platforms\limits.py`: every limit in spec 02 §5 and
   spec 08 (sizes, durations, codecs, containers, audio, frame rates, dimensions, aspect ranges,
   Instagram image rules, caption/hashtag/mention limits, YouTube title/description lengths, TikTok
   chunk rules, Instagram container poll timing, daily publish limits) as typed frozen dataclasses,
   each carrying `source` (URL or research file section) and `read_on` (date). One place, no magic
   numbers elsewhere (invariant 15).

2. **Storage port and local store** (spec 02 §2): `MediaStore` Protocol, `UploadPlan`,
   `PartReceipt`, and `LocalDiskStore` under a configurable root (`<data_dir>\media\uploads\`):
   chunked append upload with resumable state (which byte ranges/parts have arrived), size
   enforcement, SHA-256 verification on complete, `open()` with ranged reads, `signed_get_url`
   returning `None`, `delete`. Keys are `w/{workspace_id}/{asset_id}/{sha256[:16]}.{ext}`; reject
   any key or path that escapes the root (path traversal tests).

3. **R2 store** `R2Store` (spec 02 §2, §4, §6): S3-compatible multipart upload with presigned part
   URLs (the browser uploads directly), complete/abort, ranged `GET` for `open()`, presigned GET
   for `signed_get_url(key, ttl)` capped at 7 days, delete, list-and-abort of abandoned multipart
   uploads. Implement AWS Signature Version 4 yourself on top of `httpx` (already a dependency) -
   no boto3. Unit-test the signer against AWS's published SigV4 test vectors (the documented
   `examplebucket` presigned-URL example and header-signing example; put the expected values in the
   test with a comment naming the AWS doc page). All other R2Store tests use an `httpx.MockTransport`
   fake; NO real network calls and no real credentials anywhere.

4. **Probe** (spec 02 §4 security rules): run `ffprobe` (JSON output) with a timeout, only the
   `file` protocol allowed (`-protocol_whitelist file`), on a server-generated path, never a
   user-supplied argument; parse to a typed `ProbeResult` (container, codecs, duration_ms, width,
   height, fps, bitrate, audio codec/rate/channels, `moov_at_front`, `has_edit_list`); decide the
   media kind from the probe, not the filename; extract a poster frame with `ffmpeg`. Make the
   binaries configurable and give a clear error when missing. Tests: pure parsing tests on stored
   ffprobe JSON fixtures (commit small JSON fixtures, not videos), plus a few real-binary tests that
   generate tiny clips with `ffmpeg -f lavfi` into `tmp_path` and are skipped when `ffmpeg` is not on
   PATH. Detecting `moov` placement: read the top-level MP4 atoms yourself (small, bounded parser).

5. **Validation** `validate(probe, platform, post_type) -> list[Issue]` (spec 02 §5), using only
   the limits module; `Issue` has `severity` (`error` | `warning`), `code`, `message`, `fix` (e.g.
   `ffmpeg -movflags +faststart` for moov/edit-list problems). Post types: `youtube_video`,
   `youtube_short` (vertical/square + Shorts length as a labelled heuristic), `instagram_reel`,
   `instagram_image`, `instagram_carousel` child rules, `instagram_story`, `tiktok_video`. Table-
   driven tests covering every row of the spec 02 §5 table, plus "a file valid for YouTube and
   TikTok but invalid for Instagram".

6. **Delivery helpers** (spec 02 §6): a YouTube chunk iterator (8 MB, multiple of 256 KB, resumable
   from a confirmed offset), and a TikTok chunk planner implementing the Content Posting API rules
   (chunk between 5 MB and 64 MB, whole file as one chunk under 5 MB, `total_chunk_count =
   floor(size / chunk_size)` with the remainder folded into the last chunk, last chunk at most
   128 MB - check `docs\expansion\research\tiktok.md` and state the rule's source in the limits
   module). Pure functions with tests.

## Done when

All six are built with tests; `ruff check .`, `ruff format --check .`, `mypy src/runway` clean; the
full backend suite passes once under the heavy lock. Write in the report the public API of
`runway.mediastore` (classes, functions, signatures) so the next job can wire it to tables and
routes without reading all the code.

Commit in logical steps on `expansion/k-media`. Write the report as COMMON-RULES says, at
`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-media\REPORT.md`.
