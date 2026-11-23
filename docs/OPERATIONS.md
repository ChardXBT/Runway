# Operations

Use `runway init` once, `runway doctor` after dependency or configuration changes, and
`run-runway.ps1` to run the local production stack on Windows. `run-runway.sh` is a POSIX
development launcher: it starts the API and the Next.js development server, not the production
build.

## Database migrations and the worker

The API never migrates the database. Importing or starting it against a database that is behind
(or ahead of) the migration head fails with an error naming `runway db upgrade`, and uvicorn
exits non-zero. Migrate explicitly:

```powershell
.\.venv\Scripts\python.exe -m runway.cli.main db current     # read-only: current and head
.\.venv\Scripts\python.exe -m runway.cli.main db upgrade     # backup, upgrade to head, seed defaults
.\.venv\Scripts\python.exe -m runway.cli.main db downgrade 0012_creator_platforms   # backup, roll back
```

When migrations are pending, `db upgrade` first copies the database with SQLite's online backup
API to `data/backups/runway-<UTC time>-before-upgrade.db` (the newest three of these are kept;
`--no-backup` skips it). `db downgrade <revision>` always takes a backup first
(`...-before-downgrade-to-<revision>.db`, never pruned) and refuses targets older than
`0012_creator_platforms`, the schema of the pre-expansion release, unless `--force` is given.
Both refuse (exit code 3) while `runway worker` is running (`db upgrade` only when migrations are
pending; with nothing to migrate it is a harmless no-op).

`runway worker` runs queued jobs from the `publish_jobs` table. Today that is scheduled platform
sync: with `RUNWAY_PLATFORM_SYNC_INTERVAL_MINUTES` above zero it enqueues one `sync_account` job
per connected account per interval and runs it with the existing sync code. The API no longer
runs a sync thread; its `/health` reports `"sync_scheduler": "worker"` (older APIs, which synced
in-process, lack the field). The worker sleeps until the next due job (at most 30 seconds), stops
cleanly on Ctrl+C, Ctrl+Break or SIGTERM, and refuses to start a second copy for the same data
directory (exit code 3).

The worker is identified by two files, never by its command line:

- `data/runtime/worker.lock`: the worker holds an OS byte-range lock on it for its whole life.
  The OS drops the lock when the process exits, so a held lock means a live worker and a leftover
  file means nothing.
- `data/runtime/worker.pid`: `{"pid", "revision", "started_at"}`, written while holding the lock
  and removed on exit.

## How the launcher deploys

`run-runway.ps1` first asks Python for the database revision, the migration head and the
checkout's commit (`python -m runway.operations.launch_status`), and reads the running API's
`/health`. Besides the unchanged `product` and `status` fields, `/health` reports `revision`
(the commit the API was started from) and `migration_head`. The API is replaced when it is older
code: it lacks `sync_scheduler`, reports a different revision or head, or the database is behind
head. In that case the launcher:

1. checks that the process on port 8000 is this checkout's API, then stops the worker (Ctrl+Break
   on its console, PID from `worker.pid`, only after confirming that PID is running the Runway
   worker) and then the API, and waits for both to exit. Nothing is force-killed. If either does
   not stop while the API stays healthy, the older API is left serving, the database is not
   upgraded, `launcher.log` says "Deploy incomplete", and the worker is started again only if the
   API runs no in-process scheduler and expects the database's schema. If the API neither stops
   nor stays healthy, the launch fails and says so;
2. runs `db upgrade` (with its backup) and starts the API;
3. starts the worker, but only beside an API whose `/health` reports `sync_scheduler: worker`.
   The old in-process scheduler and the worker therefore never sync at the same time.

If the database is at a revision this checkout does not define (newer code migrated it and the
older code was checked out without `runway db downgrade`), the launcher stops nothing and migrates
nothing: with a healthy API it only logs the problem; without one the launch fails with the
instruction to run `runway db downgrade` from the newer checkout (see Rollback below).

A worker that cannot start (it exits within 5 seconds, or the lock check fails) never fails the
launch and never stops the API or web: the launcher logs it to `launcher.log` (output in
`data/runtime/worker.log`) and the next watchdog run retries. A worker running older code than the
checkout is restarted the same way.

## Deploy order (by hand)

1. Disable the scheduled task (`Disable-ScheduledTask -TaskName A_Runway_Task`) so the watchdog
   does not restart anything midway.
2. Stop the API (port 8000), the web server (port 3000) and `runway worker` (PID in
   `data/runtime/worker.pid`). Let running publishing or generation finish first.
3. Check out the new code, then `.\.venv\Scripts\python.exe -m runway.cli.main db upgrade`
   (it takes the backup first; keep the path it prints).
4. Start Runway with `run-runway.ps1` and re-enable the task.

Running `run-runway.ps1` after step 3 of a plain `git pull` does steps 2-4 for the API and worker
itself, as described above.

## Rollback order

Roll the schema back with the NEW checkout, because only it contains the downgrade steps:

1. Disable the scheduled task, then stop the API, the web server and the worker.
2. With the new checkout still in place:
   `.\.venv\Scripts\python.exe -m runway.cli.main db downgrade 0012_creator_platforms`
   (it takes a backup first).
3. Check out the older code, rebuild the web app if it changed, start `run-runway.ps1`, and
   re-enable the task.

## External supervisors

Any external supervisor or stop script must stop three things, not two: the API on port 8000,
the web server on port 3000, and `runway worker`, which has no port. Find the worker through
`data/runtime/worker.pid` (and confirm it is alive with the `worker.lock` check above), and stop
it with Ctrl+Break on its console, Ctrl+C or SIGTERM. Starting should still go through
`run-runway.ps1`, which starts the worker.

## Windows production startup

Install the Python environment and run `npm ci` followed by `npm run check` before
launching. The Windows launcher uses the built Next.js server, not a development
server. Rebuild after frontend updates while the application is stopped.

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\run-runway.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_windows_task.ps1
Start-ScheduledTask -TaskName A_Runway_Task
```

A new installation registers a task for the current user that starts Runway 30 seconds after
logon and then reruns the launcher every 5 minutes (`-WatchdogMinutes 0` disables the
repetition). Each run is idempotent: healthy current services are left alone, a component whose
port is free (for example after a crash, sleep, or reboot) is started again, a healthy API that is
older code than the checkout is asked to stop and restarted (see "How the launcher deploys"), and
nothing is ever killed.
Runs never overlap (`IgnoreNew`), failed runs are retried three times a minute apart, and a run
is stopped after 10 minutes; the launcher's own waits end well before that.

The installer updates an existing task while preserving its account, triggers,
enabled state, and restart policy. If the task has no logon trigger, the watchdog trigger is
added beside the existing ones. It removes unnecessary elevation and network
availability gating, and bounds the run time. Updating an old elevated task requires
Administrator PowerShell once. The task never triggers publishing or generation.
After replacing an elevated task, stop its old services from an administrator
session before relaunching at normal privilege. Do not interrupt active generation
or publishing work.

To keep Runway stopped, disable the task first (`Disable-ScheduledTask -TaskName A_Runway_Task`)
and only then stop the services; otherwise the watchdog starts them again. Disable it the same
way while running `npm run web:build`, then re-enable it.

Manual launches and Task Scheduler both call `run-runway.ps1`. No other process supervisor is
configured in this repository; any external supervisor should call the launcher the same way
rather than starting the servers directly.
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
`api.stderr.log`, `web.stdout.log`, and `web.stderr.log`. Before a component is started again,
its previous logs are kept as `*.previous.log`, so the output of a crashed server survives the
restart. `launcher.log` rotates to `launcher.previous.log` at about 1 MB. Failed startup rolls back
only children started by that invocation. An existing owned but unhealthy service
is allowed the readiness timeout (90 seconds by default) before startup fails with
diagnostics. It is deliberately not force-killed: inspect its logs and confirm no
work remains before stopping it. Reinvoke the launcher after the process exits;
there is no stale PID file or lock-directory cleanup requirement. The launcher itself is
one-shot; crash recovery comes from the scheduled task rerunning it.

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

Choose `Sign in with ChatGPT`. `agent status` must report `codex-chatgpt`, `gpt-6-luna` with high
reasoning (or your configured override), and `paid_api_fallback_enabled: false`. Runway strips
API-key credentials from model subprocesses and stops when included usage is exhausted. It never
switches to paid API billing.

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

The code repository is public, so routine maintenance does not publish a database release.
Back up the database with SQLite's online backup API (see
[`INTELLIGENCE_OPERATIONS.md`](INTELLIGENCE_OPERATIONS.md)) to a location outside Git.

`scripts\sync_private_database_release.ps1` exists only for a private repository:

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
