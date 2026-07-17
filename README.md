# Leeway

Leeway is a local-only intelligence and planning application for image-based YouTube Community
posts on the Qlob channel. It captures an historical catalogue through an explicitly initiated,
read-only browser session; builds a reproducible style profile and local retrieval index; ranks
provenance-preserving image candidates; generates retrieval-grounded caption options; and manages
a restart-safe ten-day approval queue.

Live YouTube publishing is intentionally disabled. An approved proposal can only be moved to an
internal scheduled state. No setup, test, fixture workflow, or model run logs into Google or posts
to YouTube.

The real Qlob catalogue, retrieval profile, bounded live discovery, one-proposal review flow, and
restart persistence were validated on 2026-07-17 without posting. See
`docs/QLOB_PRODUCTION_VALIDATION.md` for the exact evidence and limitations.

## Architecture

- FastAPI, Typer, SQLAlchemy, Alembic, SQLite WAL, Pillow, and NumPy in `src/leeway/`.
- Next.js, TypeScript, and Tailwind in `apps/web/`.
- Canonical metadata in `data/leeway.db`; original media, previews, reports, and diagnostics under
  `data/`.
- The deterministic `MockAgentRuntime` supports offline fixtures. Real analysis uses the official
  project-local Codex CLI with ChatGPT-plan authentication, real image inputs, Luna with low
  reasoning by default, and Pydantic-validated structured output.
- The Codex runtime removes API-key credentials from its subprocess environment and never falls
  back to the separately billed OpenAI API.
- Fixture, manual URL, experimental headed-browser, and optional API search providers share one
  provider interface.

## Windows setup

```powershell
cd C:\path\to\LeeWay
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install
.\.venv\Scripts\leeway.exe init
.\.venv\Scripts\leeway.exe doctor
```

For a real headed capture, install the Playwright browser once:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Start both local services with `./run-leeway.ps1`, then open
`http://127.0.0.1:3000`. The API binds to `127.0.0.1:8000`.

## Offline fixture workflow

```powershell
.\.venv\Scripts\leeway.exe init
.\.venv\Scripts\leeway.exe capture youtube-posts --fixture --yes
.\.venv\Scripts\leeway.exe catalog verify
.\.venv\Scripts\leeway.exe analyze history --resume
.\.venv\Scripts\leeway.exe profile build
.\.venv\Scripts\leeway.exe profile evaluate
.\.venv\Scripts\leeway.exe discover images --days 10 --dry-run
.\.venv\Scripts\leeway.exe generate batch --days 10
.\.venv\Scripts\leeway.exe queue status
```

This path is deterministic, uses synthetic images and the mock runtime, and requires no network or
external credentials.

## First real Qlob capture

After reviewing `docs/CAPTURE.md` and installing Chromium:

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --channel-url "https://www.youtube.com/@Qlob/posts" --headed --resume
```

Leeway prints the dedicated profile path, asks for confirmation, opens a visible browser, and
pauses for manual authentication, consent, account selection, or CAPTCHA handling. Press
`Ctrl+C` once to stop safely; checkpoints are committed every ten unique posts by default. Run the
same command with `--resume` to continue. Diagnostics are written to `data/snapshots/` and capture
state is stored in SQLite.

Inspect the local result with:

```powershell
.\.venv\Scripts\leeway.exe capture status
.\.venv\Scripts\leeway.exe catalog status
.\.venv\Scripts\leeway.exe catalog list --limit 20
.\.venv\Scripts\leeway.exe catalog show 1
.\.venv\Scripts\leeway.exe catalog verify
```

Verification writes JSON and Markdown reports under `data/reports/`.

## Model runtime

Copy `.env.example` to `.env`. The recommended real configuration is:

```dotenv
LEWAY_AGENT_RUNTIME=codex
LEWAY_CODEX_MODEL=gpt-5.6-luna
LEWAY_CODEX_REASONING_EFFORT=low
OPENAI_API_KEY=
OPENAI_MODEL=
```

`npm install` installs the official Codex CLI inside this repository. Authenticate it with the
ChatGPT account that has the paid Codex allowance, then verify the configuration:

```powershell
.\.venv\Scripts\leeway.exe agent login
.\.venv\Scripts\leeway.exe agent status
.\.venv\Scripts\leeway.exe agent smoke
```

Every Codex call is ephemeral, serialized, read-only, web-search-disabled, and run outside the
repository workspace. API-key environment variables are removed. LeeWay requires the CLI to report
`Logged in using ChatGPT`; API-key authentication, expired authentication, invalid output,
timeouts, or an included-usage limit stop the current batch. There is no provider fallback.

This is retrieval-based adaptation rather than model-weight fine-tuning. Qlob history, images,
captions, annotations, corrections, rejections, and accepted examples remain in the local SQLite
database. Each task receives only a bounded package of relevant examples. Historical annotation,
candidate analysis, and caption generation also receive the actual local image.

Keep `LEWAY_AGENT_RUNTIME=mock` only for the deterministic offline fixture workflow. An explicit
`openai` adapter remains available for development, but it is never selected or used as a fallback
from the configured Codex runtime.

## Discovery

The fixture provider is the default. Manual URLs are downloaded only when explicitly supplied.
The headed browser provider is experimental and disabled until
`LEWAY_ENABLE_BROWSER_SEARCH=true`; start it explicitly with:

```powershell
.\.venv\Scripts\leeway.exe discover images --days 10 --provider browser --live
```

It preserves page/image URLs and stops on challenges. It contains no stealth, CAPTCHA-solving,
proxy rotation, or identity-evasion behavior. Internet images are never auto-published, and rights
status defaults to `unknown` until reviewed.

## Known limitations

- YouTube may expose only relative publication text; Leeway records `relative` precision and does
  not invent exact dates.
- YouTube DOM capture is inherently fragile. Selectors are versioned and failures produce local
  snapshots, but a layout change can require an adapter update.
- Browser discovery quality and source rights vary by provider; all candidates require review.
- The real capture and a bounded six-candidate browser-discovery pass are verified; wider
  production searches can still encounter provider challenges or layout changes.
- Adding a caption does not by itself prove that an internet image is reusable. Rights remain
  `unknown` until human review; likely fan art and independently created artwork are rejected.
- Exact historical image-to-caption recovery measured 0/40 on the Qlob holdout, so generated
  captions remain suggestions requiring human judgment.
- Live YouTube scheduling and publishing are not implemented.

See `docs/COMPLETION_GUIDE.md` for the remaining path to production,
`docs/OPERATIONS.md` for routine commands, `docs/BUILD_REPORT.md` for the fixture proof, and
`docs/QLOB_PRODUCTION_VALIDATION.md` for the real-data pass.
