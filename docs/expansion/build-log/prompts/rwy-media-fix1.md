Job rwy-media: an independent review of your branch found the problems below. Fix all of them in
the same worktree (`%USERPROFILE%\Downloads\RunWay-k-media`, branch `expansion/k-media`), same
rules (COMMON-RULES.md). Each fix gets a test that fails before the fix. Then run ruff, ruff format,
mypy, the mediastore/limits tests and the fast unit set (COMMON-RULES rule 7; no full suite), and rewrite
REPORT.md (first line the verdict) with a "Review fixes" section listing each item and its test.

1. IMPORTANT (security, reproduced with real ffmpeg 7.1.1): `-protocol_whitelist file` does not stop
   the HLS demuxer from following `file:` entries in an uploaded `.m3u8`, so `probe()` returned
   another workspace's video metadata and `extract_poster` wrote a frame of it
   (`probe.py` ~170-183 and ~219-241; `keys.py` ~97-102 keeps the client's extension; `limits.py`
   ~231 lets YouTube accept any container). Fix: pass
   `-format_whitelist mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,image2,jpeg_pipe,png_pipe,webp_pipe,gif`
   (adjust to exactly what the validation table accepts) to BOTH ffprobe and ffmpeg; probe and
   extract from a server-named path with a fixed extension chosen by the server, never the client's
   extension; give YouTube an explicit allowed-container list. Test: a crafted `.m3u8` (and a
   `concat` list) pointing at another file must be refused by probe and poster extraction.
2. IMPORTANT: probe errors expose absolute server paths (`probe.py` ~157-160 keeps the last stderr
   line, which starts with `file:C:\...`), although the docstring calls the message safe to show.
   Strip or replace paths in every stored/returned message; fix `test_probe_timeout_and_failure`,
   which currently asserts on a message containing a path.
3. IMPORTANT (spec 02 §4 step 3): `R2Store.complete_upload` never verifies SHA-256; `plan.sha256`
   and the key's hash prefix are only the client's claim. Add verification: either per-part
   `x-amz-checksum-sha256` signed into the part URLs, or a server-side streaming hash during the
   probe job's download, rejecting the asset on mismatch. Expose it so the engine's probe job can
   call it. Test the mismatch path.
4. Workspace binding: `open`, `signed_get_url` and `delete` accept any well-formed key. Add an
   explicit `workspace_id` argument (or a scoped store view) and refuse keys whose `w/{id}/` prefix
   differs. Make the id pattern case-normalized (lowercase only) so `w/A/` and `w/a/` cannot alias on
   Windows. Update the docstring claim to match.
5. `mp4atoms.py` ~131-141/163: when the box budget is exhausted, return "unknown" (None) for
   `moov_at_front` / `has_edit_list` instead of False, and make validation emit a warning for
   unknown. Test with ftyp + moov(2-entry elst) + mdat + 20,000 `free` boxes.
6. Single-entry edit lists are ignored on purpose (`mp4atoms.py` ~109-128). Keep that only if you can
   cite why; otherwise treat any `elst` with entries as an edit list for Instagram. Record the choice
   and its source in `limits.py` and the report.
7. LocalDiskStore concurrency (`local.py` ~81-83, ~276-287): locks live on the instance. Use a
   per-upload file lock (the repo already uses file locks for tokens) so separate store instances and
   processes are safe; write the state file atomically with a unique temp name; handle Windows
   `PermissionError` on `os.replace` with a short retry.
8. Stale-upload cleanup uses start time; use last activity. Also remove orphan `.part` files with no
   state file and leftover temp state files.
9. `r2.py` ~135 stores the client's Content-Type. Accept only `video/*` / `image/*` at plan time, and
   set `response-content-type` from the probed type plus `response-content-disposition: attachment`
   on signed GETs used for previews (not on the Instagram fetch URL, which must stay plain).
10. TikTok planner (`delivery.py` ~170-186) returns one 100 MB chunk for a 100 MB file with a 64 MB
    chunk size. Re-read `docs\expansion\research\tiktok.md` and the media transfer guide facts in
    `docs\expansion\research\recheck-20261122.md` §3 and make sure no non-final chunk exceeds 64 MB and
    files above 64 MB are split; add boundary tests.
11. `r2.py` ~384-387: if the server ignores Range and answers 200, do not read the whole object;
    stream and stop at the requested length or raise. A completion that gets 404 because a parallel
    completion already finished should check the object exists and return normally.
