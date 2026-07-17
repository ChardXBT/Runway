# Operations

Use `leeway init` once, `leeway doctor` after dependency or configuration changes, and
`run-leeway.ps1` (Windows) or `run-leeway.sh` (POSIX) to run the local application.

## Codex runtime

The real model runtime uses the project-local Codex CLI and the user's included ChatGPT/Codex
allowance:

```powershell
.\.venv\Scripts\leeway.exe agent login
.\.venv\Scripts\leeway.exe agent status
.\.venv\Scripts\leeway.exe agent smoke
```

Choose `Sign in with ChatGPT`. `agent status` must report `codex-chatgpt`, the configured low- or
medium-reasoning model, and `paid_api_fallback_enabled: false`. LeeWay strips API-key credentials
from model subprocesses and stops when included usage is exhausted. It never switches to paid API
billing.

Only one model request runs at a time. Completed records remain resumable if a limit or transient
failure stops a run.

## One-time data preparation

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCQ-nHijGwxNU3Go_wyLQ5Ng/posts" --headed --resume
.\.venv\Scripts\leeway.exe catalog verify
.\.venv\Scripts\leeway.exe analyze history --resume
.\.venv\Scripts\leeway.exe profile build
.\.venv\Scripts\leeway.exe profile evaluate
```

Capture is explicit and read-only. Sign into the Google account that has Editor or Editor (Limited)
access to Qlob. The dedicated browser profile retains the local session until Google expires it.

## Daily editorial workflow

1. Open `http://127.0.0.1:3000/review`.
2. Inspect the image and edit the caption inline if needed.
3. Select `Approve & schedule`, `Reject`, or `Another image`.
4. Continue for as many options as desired; the next decision loads immediately.

Approvals are assigned the first open 10:00 AM `America/Toronto` slot. There is no fixed horizon,
and the allocator reserves at most one LeeWay-generated post per local day. Manual posts made
outside LeeWay do not consume a LeeWay slot.

The review tray refills from already accepted candidates first. If none remain, visible bounded
image discovery runs and then caption generation resumes. Model-usage exhaustion stops refill
without changing completed approvals.

Edits and approvals are positive feedback. Rejections and image replacements are negative
feedback. All signals are append-only and enter later retrieval/ranking automatically; there is no
separate training form or save step.

## YouTube publisher

Set up the dedicated publisher profile once:

```powershell
.\.venv\Scripts\leeway.exe publisher login
.\.venv\Scripts\leeway.exe publisher status
```

Use the Google account YouTube identifies as an Editor for Qlob. The ignored profile lives at
`data/browser-profile/publisher`.

With `LEWAY_PUBLISHING_ENABLED=true`, the `Approve & schedule` decision adds the exact approved
payload to a persisted FIFO outbox. One visible-browser worker schedules items serially while the
UI advances immediately.

If Google requires sign-in, LeeWay stops before touching the composer and shows the queue as
paused. Sign in through `publisher login`, then select `Resume scheduling` in Review. If a final
Schedule click produced an ambiguous result, verify the proposal instead of retrying it.

The older `publisher prepare` / `publisher confirm` commands remain for diagnostics. They are not
part of the normal UI workflow.

## Backups and evidence

Routine reports are under `data/reports/`; capture diagnostics are under `data/snapshots/`; browser
publisher screenshots are under the configured publisher screenshot directory. Runtime data,
media, browser sessions, and `.env` are ignored by Git.

SQLite uses WAL. Stop LeeWay before copying the database and its `-wal`/`-shm` companions, or use
SQLite's backup API.
