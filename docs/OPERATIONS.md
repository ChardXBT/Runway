# Operations

Use `runway init` once, `runway doctor` after dependency or configuration changes, and
`run-runway.ps1` (Windows) or `run-runway.sh` (POSIX) to run the local application.

## Codex runtime

The real model runtime uses the project-local Codex CLI and the user's included ChatGPT/Codex
allowance:

```powershell
.\.venv\Scripts\runway.exe agent login
.\.venv\Scripts\runway.exe agent status
.\.venv\Scripts\runway.exe agent smoke
```

Choose `Sign in with ChatGPT`. `agent status` must report `codex-chatgpt`, the configured low- or
medium-reasoning model, and `paid_api_fallback_enabled: false`. RunWay strips API-key credentials
from model subprocesses and stops when included usage is exhausted. It never switches to paid API
billing.

Only one model request runs at a time. Completed records remain resumable if a limit or transient
failure stops a run.

## One-time data preparation

```powershell
.\.venv\Scripts\runway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCQ-nHijGwxNU3Go_wyLQ5Ng/posts" --headed --resume
.\.venv\Scripts\runway.exe catalog verify
.\.venv\Scripts\runway.exe analyze history --resume
.\.venv\Scripts\runway.exe profile build
.\.venv\Scripts\runway.exe profile evaluate
```

Capture is explicit and read-only. Sign into the Google account that has Editor or Editor (Limited)
access to Qlob. The dedicated browser profile retains the local session until Google expires it.

## Daily editorial workflow

1. Open `http://127.0.0.1:3000/review`.
2. Inspect the image and edit the caption inline if needed.
3. Select `Reject`, open `Edit` when needed, or `Accept`.
4. Continue for as many options as desired; the next decision loads immediately.

Approvals are assigned the first open 10:00 AM `America/Toronto` slot. There is no fixed horizon,
and the allocator reserves at most one RunWay-generated post per local day. Manual posts made
outside RunWay do not consume a RunWay slot.

The review tray refills from already accepted candidates first. If none remain, visible bounded
image discovery runs and then caption generation resumes. Model-usage exhaustion stops refill
without changing completed approvals.

Edits and approvals are positive feedback. Rejections and image replacements are negative
feedback. All signals are append-only and enter later retrieval/ranking automatically; there is no
separate training form or save step.

## YouTube publisher

Set up the dedicated publisher profile once:

```powershell
.\.venv\Scripts\runway.exe publisher login
.\.venv\Scripts\runway.exe publisher status
```

The command opens ordinary installed Google Chrome. Use the Google account
YouTube identifies as an Editor for Qlob, confirm the Qlob Posts page, close
that Chrome window, and then press Enter. The ignored isolated profile lives at
`data/browser-profile/publisher`. RunWay does not automate Google credentials
or bypass account warnings.

With `RUNWAY_PUBLISHING_ENABLED=true`, the `Accept` decision adds the exact accepted
payload to a persisted FIFO outbox. One visible-browser worker schedules items serially while the
UI advances immediately.

If Google requires sign-in, RunWay stops before touching the composer and shows the queue as
paused. Sign in through `publisher login`, then select `Resume scheduling` in Review. If a final
Schedule click produced an ambiguous result, verify the proposal instead of retrying it.

The older `publisher prepare` / `publisher confirm` commands remain for diagnostics. They are not
part of the normal UI workflow.

## Backups and evidence

Routine reports are under `data/reports/`; capture diagnostics are under `data/snapshots/`; browser
publisher screenshots are under the configured publisher screenshot directory. Runtime data,
media, browser sessions, and `.env` are ignored by Git.

SQLite uses WAL. Stop RunWay before copying the database and its `-wal`/`-shm` companions, or use
SQLite's backup API.

## Intelligence lifecycle

Schema migration, online backup, exact representation planning/backfill,
activation and rollback, feedback reconciliation, annotation refresh, persisted
preference models, provider gates, blind studies, active learning, and the
intelligence database doctor are documented in
[`INTELLIGENCE_OPERATIONS.md`](INTELLIGENCE_OPERATIONS.md).

Before any production-data intelligence maintenance, explicitly set:

```powershell
$env:RUNWAY_DATA_DIR = "data/qlob-production"
$env:RUNWAY_PUBLISHING_ENABLED = "false"
$env:RUNWAY_AGENT_RUNTIME = "mock"
```

The final health commands are:

```powershell
.\.venv\Scripts\runway.exe database schema-verify
.\.venv\Scripts\runway.exe database intelligence-doctor
```

The doctor exits nonzero for a critical integrity, provenance, isolation,
activation, model, or safety violation.
