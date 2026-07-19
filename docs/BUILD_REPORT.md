# Build report

The initial fixture build completed and was verified on 2026-07-16. The milestone detail below
records that deterministic baseline. A real Qlob capture, ChatGPT-authenticated Codex analysis,
bounded live image search, real proposal, UI pass, and restart check were subsequently completed on
2026-07-17 without external publication. See `docs/QLOB_PRODUCTION_VALIDATION.md` for the exact
production evidence.

The completed source repository is privately hosted at
`https://github.com/ChardXBT/Runway`, with `main` as the default branch. GitHub visibility was
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
- Checks at that checkpoint: Ruff passed, Mypy strict passed, Pytest `47 passed`, ESLint passed,
  Vitest `5 passed`, the Next.js production build passed, and both npm audits found zero known
  vulnerabilities.
- Captured and verified all 771 posts exposed by Qlob's Community surface, annotated all 698
  image+caption records, and built 243,253 similarity edges.
- Built real style profile v4, exercised a bounded six-candidate live search, and generated one
  `needs_review` proposal that survived API and web restarts without any YouTube action.

## 2026-07-17 completion update

- Added nine-candidate question-first generation, grounded visible-emotion prompts, deterministic
  reranking, and an explicit open-question/observation/reaction review mix.
- Added append-only feedback memory for edits, alternatives, preferences, approvals, and
  rejections, including reason codes and image verdicts.
- Replaced the original ten-frame queue with a continuous one-decision editorial conveyor. Caption
  edits are inline; approve, reject, and image replacement record learning and load the next option.
- Added first-open-day allocation at 10:00 AM Toronto time, with a strict one-Runway-post-per-day
  invariant and no fixed scheduling horizon.
- Implemented accept-to-schedule publishing through a persisted serial outbox with Qlob/Editor
  checks, payload hashing, session-expiry pause/resume, screenshots, Scheduled-tab verification,
  and conservative no-retry recovery after an ambiguous final click.
- Source metadata remains preserved but is no longer an approval-form gate.
- Added Alembic migrations `0004_feedback_and_publisher` and `0005_editorial_conveyor`.
- Current local checks: Ruff lint/format passed, Mypy strict passed, Pytest 51 passed, ESLint passed,
  Vitest 6 passed, the Next.js production build passed, and both npm audits found zero known
  vulnerabilities.
- No real YouTube Schedule/Post button was clicked. External Qlob acceptance remains a separate
  user-authorized action.

## Milestone delivery

### Milestone 0 — foundation

- Created the typed Python package, Typer CLI, FastAPI application, Next.js review app, local
  configuration, structured logging, and Windows/POSIX launchers.
- Added SQLAlchemy models for the initial domain, SQLite WAL, initial Alembic migrations `0001` and
  `0002`, restart-safe repositories, local file storage, and audit events. Later milestones add
  migrations `0003` and `0004`.
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

### Milestone 4 — editorial conveyor and guarded publishers

- Added a restart-safe, DST-aware first-open-day allocator with no fixed horizon and no more than
  one Runway-generated post per Toronto local date.
- Added the focused Review conveyor, uncapped Schedule list, Archive, Settings, and Activity
  evidence surfaces. The primary decision supports inline editing, alternatives, approve, reject,
  and image replacement.
- Added an internal publisher boundary plus a separate persisted FIFO outbox and guarded
  visible-browser scheduler. No model path can approve or publish; the operator's exact
  `Accept` action is the proposal-specific instruction.

## Repository tree

```text
Runway/
├── .github/workflows/ci.yml
├── alembic/                     # database migrations
├── apps/
│   ├── api/                     # FastAPI entry point
│   └── web/                     # Next.js UI, tests, and web lockfile
├── data/                        # ignored runtime data; README/.gitkeep tracked
├── docs/                        # product, architecture, operations, security, decisions
├── scripts/e2e_demo.py          # isolated fixture proof runner
├── src/runway/
│   ├── analysis/                # runtimes, prompts, schemas, analysis
│   ├── api/                     # local HTTP routes
│   ├── capture/                 # fixture and headed-browser capture
│   ├── catalog/                 # inspection and verification
│   ├── discovery/               # provider interface and implementations
│   ├── intelligence/            # profile, evaluation, retrieval
│   ├── media/                   # storage, fingerprints, previews
│   ├── proposals/               # options, feedback, and daily-slot allocation
│   ├── editorial/               # continuous review-tray orchestration
│   ├── publishing/              # internal + guarded visible-browser publisher
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
cd C:\path\to\Runway
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\runway.exe init
.\.venv\Scripts\runway.exe doctor
```

`npm install` installs the isolated web package from `apps/web/package-lock.json`. Run
`./run-runway.ps1`, then open `http://127.0.0.1:3000`. Both services bind only to loopback.

## First Qlob capture

```powershell
.\.venv\Scripts\runway.exe capture youtube-posts --channel-url "https://www.youtube.com/@Qlob/posts" --headed --resume
```

Runway prints the dedicated profile path and asks for confirmation before opening a visible
browser. Handle sign-in, consent, account choice, or a challenge yourself. Press `Ctrl+C` once to
stop safely, then rerun the same command with `--resume`. Capture state remains in `data/runway.db`;
HTML, screenshot, and JSON failure diagnostics are saved under `data/snapshots/`.

## Catalogue inspection and verification

```powershell
.\.venv\Scripts\runway.exe capture status
.\.venv\Scripts\runway.exe catalog status
.\.venv\Scripts\runway.exe catalog list --limit 20
.\.venv\Scripts\runway.exe catalog show 1
.\.venv\Scripts\runway.exe catalog verify
```

Verification reports are written to `data/reports/catalog-verification.json` and
`data/reports/catalog-verification.md`.

## Analysis and profile generation

```powershell
.\.venv\Scripts\runway.exe analyze history --resume
.\.venv\Scripts\runway.exe profile build
.\.venv\Scripts\runway.exe profile evaluate
```

Profile and evaluation evidence is written under `data/reports/`. Corrections stay in separate
overlay records and are applied when a profile is rebuilt.

## Historical fixture demo

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
- the original fixture created 10 proposals across 10 days at 10:00 in `America/Toronto`;
- caption edits and internal scheduling survived a database restart; and
- the internally scheduled proposal had no external publication ID.

To operate the same stages individually:

```powershell
.\.venv\Scripts\runway.exe capture youtube-posts --fixture --yes
.\.venv\Scripts\runway.exe catalog verify
.\.venv\Scripts\runway.exe analyze history --resume
.\.venv\Scripts\runway.exe profile build
.\.venv\Scripts\runway.exe profile evaluate
.\.venv\Scripts\runway.exe discover images --days 10 --dry-run
.\.venv\Scripts\runway.exe generate batch --days 10
.\.venv\Scripts\runway.exe queue status
```

## Historical fixture verification results

- Ruff lint: passed.
- Ruff format check: 75 files formatted.
- Mypy strict mode: 59 source files, no issues.
- Pytest: 47 passed, including capture hardening, resumable batch analysis, discovery safeguards,
  proposal grounding persistence, and the full offline workflow.
- ESLint: passed.
- Vitest: 2 component test files and 5 tests passed.
- Next.js production build: passed; all 9 routes compiled and TypeScript passed.
- Browser smoke: all seven application pages rendered against the real Qlob database. The review
  proposal, safeguards, catalogue, profile, queue, and audit history remained intact after both
  services restarted.
- `runway doctor`: Python, Node, npm, Playwright Chromium, paths, migration `0004`, mock runtime,
  and loopback API checks passed.
- npm audits: 0 known vulnerabilities in both lockfiles. Next.js' inherited PostCSS version is
  overridden to the patched `8.5.19` release in the isolated web package.

## Honest limitations

- The real capture, Codex runtime, and a bounded browser-discovery pass are verified. Search
  providers can still challenge, throttle, or change markup; Runway stops instead of bypassing
  those controls.
- Rights status for internet candidates defaults to `unknown`; it is retained as diagnostic
  metadata and does not add a checkbox to the fast approval path.
- YouTube may expose relative dates only; Runway records that reduced precision instead of
  inventing a timestamp. YouTube DOM changes can still require selector updates.
- Exact historical image-to-caption recovery measured 0% on the real 40-post holdout; caption
  ranking measured 75%. Generated captions are reviewable suggestions, not proven matches.
- The guarded YouTube scheduler is implemented and offline-tested. Its current Qlob account/browser
  environment still needs one separately authorized real scheduling acceptance pass.
