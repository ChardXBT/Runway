# Operations

Use `runway init` once, `runway doctor` after dependency or configuration changes, and
`run-runway.ps1` (Windows) or `run-runway.sh` (POSIX) to run the local application.

## Windows production startup

Install the Python environment and run `npm ci` followed by `npm run check` before
launching. The Windows launcher uses the built Next.js server, not a development
server. Rebuild after frontend updates while the application is stopped.

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\run-runway.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_windows_task.ps1
Start-ScheduledTask -TaskName A_Runway_Task
```

The installer updates an existing task while preserving its account, triggers,
enabled state, and restart policy. It removes unnecessary elevation and network
availability gating. Updating an old elevated task requires Administrator
PowerShell once. A new installation creates an on-demand task for the current
logged-in user. It does not register an automatic publishing or generation job.
After replacing an elevated task, stop its old services from an administrator
session before relaunching at normal privilege. Do not interrupt active generation
or publishing work.

Manual launches, Task Scheduler, and Supervisor all call `run-runway.ps1`.
The launcher is independent of the caller's directory and PowerShell profile,
uses an exclusive file lock across sessions, and returns zero only after the
requested API and web services report their product identities and healthy states.
Both ports are checked before starting children. An unrelated or unverifiable
listener causes a safe failure; the launcher never kills it. A partial stack is
completed while retaining its healthy component. `-ApiOnly` and `-WebOnly` are
available for diagnostics and are mutually exclusive.

Node is located through `RUNWAY_NODE_EXE`, PATH, or the standard Program Files
installation. Python always comes from this checkout's `.venv`. No npm command,
dependency installation, network download, or build occurs at launch. Services
bind only to `127.0.0.1`, on ports 8000 and 3000. The web `/health` endpoint
identifies the frontend; callers must also check the API `/health` endpoint.

Diagnostics are in ignored `data/runtime/launcher.log`, `api.stdout.log`,
`api.stderr.log`, `web.stdout.log`, and `web.stderr.log`. Failed startup rolls back
only children started by that invocation. An existing owned but unhealthy service
is allowed the readiness timeout (90 seconds by default) before startup fails with
diagnostics. It is deliberately not force-killed: inspect its logs and confirm no
work remains before stopping it. Reinvoke the launcher after the process exits;
there is no stale PID file or lock-directory cleanup requirement. This is an
on-demand launcher, not a service watchdog; run it again after a later child crash.

For development only, use `npm run web:dev` and a separately started API.
Do not share production ports between development and production servers.

## Codex runtime

The real model runtime uses the project-local Codex CLI and the user's included ChatGPT/Codex
allowance:

```powershell
.\.venv\Scripts\runway.exe agent login
.\.venv\Scripts\runway.exe agent status
.\.venv\Scripts\runway.exe agent smoke
```

Choose `Sign in with ChatGPT`. `agent status` must report `codex-chatgpt`, the configured low- or
medium-reasoning model, and `paid_api_fallback_enabled: false`. Runway strips API-key credentials
from model subprocesses and stops when included usage is exhausted. It never switches to paid API
billing.

Only one model request runs at a time. Completed records remain resumable if a limit or transient
failure stops a run.

## One-time data preparation

```powershell
.\.venv\Scripts\runway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx/posts" --headed --resume
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

Approvals are assigned the first open default 10:00 AM `America/Toronto` slot and remain editable
in Lineup. Date and time may be changed per post; the allocator still reserves at most one
Runway-generated post per local day. Accept, edit, move, and local removal perform no YouTube work.

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

The command opens ordinary installed Google Chrome. Use the Google account whose delegated role
the channel owner configured for Qlob, confirm the Qlob Posts page, close
that Chrome window, and then press Enter. The ignored isolated profile lives at
`data/browser-profile/publisher`. Runway does not automate Google credentials
or bypass account warnings.

Assisted preparation is the default and needs no saved session:

```dotenv
RUNWAY_PUBLISHING_MODE=assisted
RUNWAY_PUBLISHING_ENABLED=false
RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=false
```

From Lineup, review the exact channel, mode, images, captions, timestamps, and timezone, then
confirm `Prepare ... for YouTube`. Runway returns an ordered session-local workspace; use its
copy/download/open actions and complete the final schedule natively in YouTube.

Authorized browser mode is disabled unless all interlocks are explicitly configured:

```dotenv
RUNWAY_PUBLISHING_MODE=authorized_browser
RUNWAY_PUBLISHING_ENABLED=true
RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=true
```

After manual login, run the read-only saved-session capability check in Settings. It observes the
expected channel and Community controls; it does not prove the exact delegated role. Only the
confirmed Lineup action creates FIFO outbox work, and the response reports queue creation rather
than claiming external success.

If Google requires sign-in, Runway stops before touching the composer and shows the queue as
paused. Sign in through `publisher login`, recheck the saved session, then resume from Lineup or
Settings. If a final
Schedule click produced an ambiguous result, verify the proposal instead of retrying it.

The older `publisher prepare` / `publisher confirm` commands remain for diagnostics. They are not
part of the normal UI workflow.

## Backups and evidence

Routine reports are under `data/reports/`; capture diagnostics are under `data/snapshots/`; browser
publisher screenshots are under the configured publisher screenshot directory. Runtime data,
media, browser sessions, and `.env` are ignored by Git.

SQLite uses WAL. Stop Runway before copying the database and its `-wal`/`-shm` companions, or use
SQLite's backup API.

After a validated commit has been pushed and `origin/main` is confirmed at that exact SHA, publish
the private recovery release:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync_private_database_release.ps1 -TargetSha <full-commit-sha>
```

This command refuses to run before the commit exists on `origin/main` or if the GitHub repository
is not private. It creates an online SQLite backup plus every media file referenced by the
database, validates all hashes and table counts, uploads the assets, downloads them again, and
performs a complete restoration verification. Browser profiles and Google authentication are
never included.

If the code repository is public, the private database release is intentionally
blocked. Do not bypass this guard or upload production data to a public release;
configure an approved private backup destination separately.

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
