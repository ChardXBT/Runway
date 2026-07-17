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
