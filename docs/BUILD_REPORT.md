# Build report

Build completed and verified on 2026-07-16. All milestone claims below are backed by local fixture
data, the mock agent runtime, and automated checks. No live Qlob crawl, OpenAI model request,
internet image search, or external publication was performed.

The completed source repository is privately hosted at
`https://github.com/ChardXBT/LeeWay`, with `main` as the default branch. GitHub visibility was
verified as `PRIVATE` after the initial push.

## 2026-07-17 runtime update

- Added an official project-local Codex CLI runtime using ChatGPT authentication, Luna with low
  reasoning, actual image inputs, Pydantic output schemas, serialized requests, and stop-on-limit
  behavior.
- Added an enforced no-paid-API boundary: API-key environment values are stripped, API-key login
  is rejected, and Codex failures never fall back to the OpenAI adapter.
- Added fan-art and personal-artwork exclusions, warnings, and hard filters.
- Added CLI status/login checks and settings UI visibility for the selected model and fallback
  policy.
- Added `docs/COMPLETION_GUIDE.md` so implemented fixture milestones are separated from pending
  real-account and live-publisher acceptance work.
- Verified saved `Sign in with ChatGPT` authentication and passed one real Luna/low structured
  image request with API fallback disabled.
- Current checks: Ruff lint/format passed, Mypy strict passed, Pytest `29 passed`, ESLint passed,
  Vitest `2 passed`, the Next.js production build passed, and both npm audits found zero known
  vulnerabilities.

## Milestone delivery

### Milestone 0 — foundation

- Created the typed Python package, Typer CLI, FastAPI application, Next.js review app, local
  configuration, structured logging, and Windows/POSIX launchers.
- Added SQLAlchemy models for the complete domain, SQLite WAL, Alembic migrations `0001` and
  `0002`, restart-safe repositories, local file storage, and audit events.
- Added deterministic mock and configuration-gated OpenAI agent runtimes. The OpenAI adapter uses
  Pydantic-validated structured output and never hard-codes a model.

### Milestone 1 — capture and catalogue

- Implemented explicit fixture, managed headed-browser, and user-supplied CDP capture paths.
- Added surface checks, challenge/authentication stops, selector confidence gates, one-time
  confirmation, transactional checkpoints, safe interruption, resume, idempotency, and diagnostic
  bundles.
- Preserved raw records and provenance separately from normalized posts and media. Added catalogue
  status/list/show/verify commands plus JSON and Markdown verification reports.

### Milestone 2 — historical intelligence

- Added SHA-256, perceptual hashes, crop-resistant descriptors, local image embeddings, quality
  features, exact/transformed/source/semantic duplicate checks, and uncertainty handling.
- Added deterministic caption features, Pydantic annotations, correction overlays that preserve
  original model output, similarity edges, a versioned style profile, local retrieval, and holdout
  evaluation.
- Added annotation, eligibility, profile, and evidence views in the local UI.

### Milestone 3 — discovery and captions

- Added pluggable fixture, manual-URL, optional API, and experimental headed-browser providers.
- Added provenance and rights fields, hard filters, duplicate suppression, explainable ranking,
  source/domain blocking, square previews, and candidate persistence.
- Added retrieval-grounded caption packages, three structured caption options, persisted model
  metadata, resumable generation runs, and proposal replacement/regeneration.

### Milestone 4 — approval queue and internal publisher

- Added a restart-safe ten-day engine using the persisted channel timezone and default local time,
  including DST-aware scheduling, conflict/gap detection, and resumable batches.
- Added dashboard, review, queue, catalogue/detail, profile, settings, and activity pages. Review
  supports caption edits, alternatives, approve/reject, regeneration, replacement, blocking,
  rescheduling, and metadata correction.
- Added an internal-only publisher boundary. Approval can become `internally_scheduled`; no network
  publication code or external post ID is produced.

## Repository tree

```text
LeeWay/
├── .github/workflows/ci.yml
├── alembic/                     # database migrations
├── apps/
│   ├── api/                     # FastAPI entry point
│   └── web/                     # Next.js UI, tests, and web lockfile
├── data/                        # ignored runtime data; README/.gitkeep tracked
├── docs/                        # product, architecture, operations, security, decisions
├── scripts/e2e_demo.py          # isolated fixture proof runner
├── src/leeway/
│   ├── analysis/                # runtimes, prompts, schemas, analysis
│   ├── api/                     # local HTTP routes
│   ├── capture/                 # fixture and headed-browser capture
│   ├── catalog/                 # inspection and verification
│   ├── discovery/               # provider interface and implementations
│   ├── intelligence/            # profile, evaluation, retrieval
│   ├── media/                   # storage, fingerprints, previews
│   ├── proposals/               # ten-day workflow
│   ├── publishing/              # internal-only publisher
│   └── ranking/                 # duplicate detection and scoring
├── tests/{unit,integration,e2e}/
├── package-lock.json            # root task-runner lockfile
├── requirements.lock            # exact verified Python environment
├── pyproject.toml
└── README.md
```

## Exact local setup

Run from PowerShell on Windows:

```powershell
cd C:\path\to\LeeWay
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\leeway.exe init
.\.venv\Scripts\leeway.exe doctor
```

`npm install` installs the isolated web package from `apps/web/package-lock.json`. Run
`./run-leeway.ps1`, then open `http://127.0.0.1:3000`. Both services bind only to loopback.

## First Qlob capture

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --channel-url "https://www.youtube.com/@Qlob/posts" --headed --resume
```

Leeway prints the dedicated profile path and asks for confirmation before opening a visible
browser. Handle sign-in, consent, account choice, or a challenge yourself. Press `Ctrl+C` once to
stop safely, then rerun the same command with `--resume`. Capture state remains in `data/leeway.db`;
HTML, screenshot, and JSON failure diagnostics are saved under `data/snapshots/`.

## Catalogue inspection and verification

```powershell
.\.venv\Scripts\leeway.exe capture status
.\.venv\Scripts\leeway.exe catalog status
.\.venv\Scripts\leeway.exe catalog list --limit 20
.\.venv\Scripts\leeway.exe catalog show 1
.\.venv\Scripts\leeway.exe catalog verify
```

Verification reports are written to `data/reports/catalog-verification.json` and
`data/reports/catalog-verification.md`.

## Analysis and profile generation

```powershell
.\.venv\Scripts\leeway.exe analyze history --resume
.\.venv\Scripts\leeway.exe profile build
.\.venv\Scripts\leeway.exe profile evaluate
```

Profile and evaluation evidence is written under `data/reports/`. Corrections stay in separate
overlay records and are applied when a profile is rebuilt.

## Fixture ten-day demo

The single proof command creates an isolated timestamped data directory and uses no network:

```powershell
.\.venv\Scripts\python.exe scripts\e2e_demo.py
```

The verified run is at `data/proofs/20260716T214847Z/`. Its 13 assertions all passed:

- capture paused and resumed the same run, and a third pass created no duplicate posts;
- 12 posts were verified, with 9 training-eligible image posts;
- 9 posts were analyzed, 36 similarity edges were built, and no analysis failed;
- profile v1 used 7 training and 2 holdout samples;
- transformed duplicate recall was 1.0 and unrelated false-positive rate was 0.0;
- discovery persisted 30 candidates, accepted 23, and hard-rejected 7;
- 10 proposals covered all 10 days at 10:00 in `America/Toronto` with no gaps;
- caption edits and internal scheduling survived a database restart; and
- the internally scheduled proposal had no external publication ID.

To operate the same stages individually:

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --fixture --yes
.\.venv\Scripts\leeway.exe catalog verify
.\.venv\Scripts\leeway.exe analyze history --resume
.\.venv\Scripts\leeway.exe profile build
.\.venv\Scripts\leeway.exe profile evaluate
.\.venv\Scripts\leeway.exe discover images --days 10 --dry-run
.\.venv\Scripts\leeway.exe generate batch --days 10
.\.venv\Scripts\leeway.exe queue status
```

## Verification results

- Ruff lint: passed.
- Ruff format check: 75 files formatted.
- Mypy strict mode: 57 source files, no issues.
- Pytest: 19 passed, including the fixture browser parser and full offline workflow.
- ESLint: passed.
- Vitest: 2 component test files and 2 tests passed.
- Next.js production build: passed; all 9 routes compiled and TypeScript passed.
- Browser smoke: all 7 application pages rendered against the local API with no post-fix console
  errors. A locale-sensitive hydration mismatch found during the pass was corrected.
- `leeway doctor`: Python, Node, npm, Playwright Chromium, paths, migration `0002`, mock runtime,
  and loopback API checks passed.
- npm audits: 0 known vulnerabilities in both lockfiles. Next.js' inherited PostCSS version is
  overridden to the patched `8.5.19` release in the isolated web package.

## Honest limitations

- The live Qlob capture adapter was implemented but not run against the user's account.
- The OpenAI adapter was implemented and schema-validated in code, but no model request was made.
- Manual/API/browser internet discovery adapters were not run; fixture discovery is the only
  verified provider. Browser discovery remains experimental and explicitly feature-gated.
- Rights status for internet candidates defaults to `unknown` and requires human review.
- YouTube may expose relative dates only; Leeway records that reduced precision instead of
  inventing a timestamp. YouTube DOM changes can still require selector updates.
- Live YouTube scheduling and publishing are intentionally not implemented. The only verified
  publisher is the local internal scheduler.
