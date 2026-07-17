# Operations

Use `leeway init` once, `leeway doctor` after dependency/configuration changes, and
`run-leeway.ps1` (Windows) or `run-leeway.sh` (POSIX) to run the local application. Capture is not
scheduled. Build a new profile explicitly after catalogue changes, then run discovery and batch
generation.

## Codex runtime

The real model runtime uses the project-local Codex CLI and the user's included ChatGPT/Codex
allowance:

```powershell
.\.venv\Scripts\leeway.exe agent login
.\.venv\Scripts\leeway.exe agent status
.\.venv\Scripts\leeway.exe agent smoke
```

Use the browser sign-in opened by `agent login` and choose `Sign in with ChatGPT`. `agent status`
must report `codex-chatgpt`, `gpt-5.6-luna`, `low`, and
`paid_api_fallback_enabled: false` before a real run. No OpenAI API key is needed. If the included
limit is exhausted, LeeWay stops the batch and preserves completed database records; wait for the
allowance to reset, then rerun a resumable command.

`agent smoke` makes one small structured-image request using a temporary synthetic image and does
not create or change the Qlob database.

Only one model request runs at a time. Do not launch overlapping analysis, discovery, or generation
commands from separate terminals.

## Real-data order

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCQ-nHijGwxNU3Go_wyLQ5Ng/posts" --headed --resume
.\.venv\Scripts\leeway.exe catalog verify
.\.venv\Scripts\leeway.exe analyze history --resume
.\.venv\Scripts\leeway.exe profile build
.\.venv\Scripts\leeway.exe profile evaluate
.\.venv\Scripts\leeway.exe discover images --days 10 --provider browser --live
.\.venv\Scripts\leeway.exe generate batch --days 10
.\.venv\Scripts\leeway.exe queue status
```

The first command uses a dedicated persistent Chromium profile. Sign into the Google account that
has Editor or Editor (Limited) access to Qlob. The browser profile retains that session locally,
subject to normal Google session expiry or security challenges.

Routine reports are written to `data/reports/`. Capture failure bundles are in `data/snapshots/`.
The SQLite database uses WAL; copy the database plus `-wal`/`-shm` files only after stopping Leeway,
or use SQLite's backup facility.

## Caption teaching

In Review, save a specific final caption and select useful reasons. Prefer:

- `Prefer an open question` when a grounded `why`, `how`, or `what` prompt would invite replies;
- `Too generic` for flat descriptions or generic engagement bait;
- `Wrong emotion` or `Wrong character` when image understanding is wrong; and
- `Invented context` when the caption assumes off-screen events.

Use `Save as preferred` for a good caption even when the proposal is not yet approved. Rejections
and approvals are recorded automatically. Feedback enters future retrieval immediately; no
separate training job is required.

## Guarded external scheduling

Normal operation:

```dotenv
LEWAY_PUBLISHING_ENABLED=false
```

One-time account setup:

```powershell
.\.venv\Scripts\leeway.exe publisher login
```

Use the Google account YouTube identifies as an Editor for Qlob. The profile is stored at
`data/browser-profile/publisher`, ignored by Git, and reused until Google expires the session.

For an explicitly authorized controlled schedule:

1. Set `LEWAY_PUBLISHING_ENABLED=true` and restart API/web.
2. Run `publisher status`.
3. Confirm the proposal is provenance-reviewed, approved, internally scheduled, and at least five
   minutes in the future.
4. Run `publisher prepare --proposal-id <ID>`. This does not submit.
5. Inspect the exact returned channel, caption, image path, and time.
6. Run `publisher confirm --attempt-id <ID>` only after final authorization; enter the hidden token
   and exact phrase.
7. If verification is inconclusive, run `publisher verify --proposal-id <ID>`. Never prepare a
   duplicate.
8. Return the feature gate to `false` and restart.

Keep a Qlob Manager available for external recovery. LeeWay does not bypass Google challenges and
does not automatically delete or retry a post.
