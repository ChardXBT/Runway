# RunWay

RunWay is a local-only intelligence and planning application for image-based YouTube Community
posts. Qlob is the configured MVP channel, while the canonical intelligence kernel is
channel-generic: it builds isolated, layered channel profiles; compiles persisted hybrid-retrieval
evidence; verifies grounded caption slates; and learns separately from image, caption, and pairing
decisions. It also manages a continuous editorial feed backed by a restart-safe scheduling
outbox. Human edits, selections, accepts, and rejections become local retrieval and preference
evidence, so later passes improve without model-weight training.

When the visible-browser publisher is enabled, the explicit `Accept` action assigns
the first open 10:00 AM Eastern day and adds the post to a persisted FIFO outbox. Browser
submissions run one at a time, and RunWay reserves at most one bot post per local day. The schedule
has no horizon cap; manually added Qlob posts are independent.

The real Qlob catalogue, retrieval profile, bounded live discovery, one-proposal review flow, and
restart persistence were validated on 2026-07-17 without posting. See
`docs/QLOB_PRODUCTION_VALIDATION.md` for the exact evidence and limitations.

## Architecture

- FastAPI, Typer, SQLAlchemy, Alembic, SQLite WAL, Pillow, and NumPy in `src/runway/`.
- Next.js, TypeScript, and Tailwind in `apps/web/`.
- A responsive Runway with one current image/caption decision, an uncapped Lineup calendar,
  searchable 60-record archive pages, profile diagnostics, and an immutable activity inspector.
- Canonical metadata in `data/runway.db`; original media, previews, reports, and diagnostics under
  `data/`.
- The deterministic `MockAgentRuntime` supports offline fixtures. Real analysis uses the official
  project-local Codex CLI with ChatGPT-plan authentication, real image inputs, Luna with low
  reasoning by default, and Pydantic-validated structured output.
- The Codex runtime removes API-key credentials from its subprocess environment and never falls
  back to the separately billed OpenAI API.
- Caption planning follows the configured channel policy. Generation, independent grounding
  verification, pairwise preference scoring, diversity selection, one bounded retry, and typed
  abstention form one canonical caption pipeline.
- Every considered retrieval item, complete generated caption slate, displayed exposure, edit,
  pairwise choice, and separate image/caption/pairing signal is persisted with provenance.
- Versioned deterministic image, text, multimodal, and multi-image representations work fully
  offline. Provider registries require explicit selection and never fall back to a paid service.
- Reference-image-plus-text retrieval combines visual evidence, instructions, structured
  constraints, source policy, and rights policy without downloading model weights.
- A safe image-generation boundary records eligibility and complete lineage. Only the
  deterministic mock provider is enabled; paid and unknown-rights generation paths fail closed.
- The external scheduler uses a separate visible persistent profile, a disabled-by-default feature
  gate, a persisted serial outbox, payload hashing, screenshots, and conservative no-retry
  recovery.
- Fixture, manual URL, experimental headed-browser, and optional API search providers share one
  provider interface.

## Windows setup

```powershell
cd C:\path\to\RunWay
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install
.\.venv\Scripts\runway.exe init
.\.venv\Scripts\runway.exe doctor
```

For a real headed capture, install the Playwright browser once:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Start both local services with `./run-runway.ps1`, then open
`http://127.0.0.1:3000`. The API binds to `127.0.0.1:8000`.

## Offline fixture workflow

```powershell
.\.venv\Scripts\runway.exe init
.\.venv\Scripts\runway.exe capture youtube-posts --fixture --yes
.\.venv\Scripts\runway.exe catalog verify
.\.venv\Scripts\runway.exe analyze history --resume
.\.venv\Scripts\runway.exe profile build
.\.venv\Scripts\runway.exe profile evaluate
.\.venv\Scripts\runway.exe discover images --days 10 --dry-run
.\.venv\Scripts\runway.exe generate batch --days 5
.\.venv\Scripts\runway.exe queue status
```

This path is deterministic, uses synthetic images and the mock runtime, and requires no network or
external credentials.

## Canonical intelligence verification

The pre-upgrade engine is frozen immutably at commit
`876fe5f814b1a58f0d11b9eaf29f4c508f20595d`. The canonical replacement uses one production
retrieval/caption path; there is no runtime V1/V2 selector or shadow engine. Verify and inspect the
saved evidence without invoking a model or publisher:

```powershell
.\.venv\Scripts\runway.exe intelligence baseline
.\.venv\Scripts\runway.exe intelligence experiments
.\.venv\Scripts\runway.exe retrieval inspect --run-id 1
.\.venv\Scripts\runway.exe embeddings status
.\.venv\Scripts\runway.exe images providers
```

The bounded tuning ledger, one-time sealed holdout, five-channel generalization run, ablations,
critical-gate JUnit output, and replacement decision are under
`benchmarks/intelligence/`. See `docs/INTELLIGENCE_REPLACEMENT_REPORT.md` for the exact
architecture, migration, metrics, uncertainty, tests, rollback, and remaining limitations.

## First real Qlob capture

After reviewing `docs/CAPTURE.md` and installing Chromium:

```powershell
.\.venv\Scripts\runway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCQ-nHijGwxNU3Go_wyLQ5Ng/posts" --headed --resume
```

RunWay prints the dedicated profile path, asks for confirmation, opens a visible browser, and
pauses for manual authentication, consent, account selection, or CAPTCHA handling. Press
`Ctrl+C` once to stop safely; checkpoints are committed every ten unique posts by default. Run the
same command with `--resume` to continue. Diagnostics are written to `data/snapshots/` and capture
state is stored in SQLite. A live run can reopen its latest completed checkpoint because a
temporary YouTube continuation plateau is not proof of the end. Completion requires at least 30
unchanged probes and 90 sustained seconds with no new card count or tail post ID.

Inspect the local result with:

```powershell
.\.venv\Scripts\runway.exe capture status
.\.venv\Scripts\runway.exe catalog status
.\.venv\Scripts\runway.exe catalog list --limit 20
.\.venv\Scripts\runway.exe catalog show 1
.\.venv\Scripts\runway.exe catalog verify
```

Verification writes JSON and Markdown reports under `data/reports/`.

## Model runtime

Copy `.env.example` to `.env`. The recommended real configuration is:

```dotenv
RUNWAY_AGENT_RUNTIME=codex
RUNWAY_CODEX_MODEL=gpt-5.6-luna
RUNWAY_CODEX_REASONING_EFFORT=low
OPENAI_API_KEY=
OPENAI_MODEL=
```

`npm install` installs the official Codex CLI inside this repository. Authenticate it with the
ChatGPT account that has the paid Codex allowance, then verify the configuration:

```powershell
.\.venv\Scripts\runway.exe agent login
.\.venv\Scripts\runway.exe agent status
.\.venv\Scripts\runway.exe agent smoke
```

Every Codex call is ephemeral, serialized, read-only, web-search-disabled, and run outside the
repository workspace. API-key environment variables are removed. RunWay requires the CLI to report
`Logged in using ChatGPT`; API-key authentication, expired authentication, invalid output,
timeouts, or an included-usage limit stop the current batch. There is no provider fallback.

This is retrieval- and preference-based adaptation rather than model-weight fine-tuning. Channel
history, images, captions, annotations, corrections, caption edits, explicit policies,
preferences, rejections, and accepted examples remain in the local SQLite database. Each task
receives only a bounded, channel-isolated evidence package. Historical annotation, candidate
analysis, and caption generation receive all relevant local images. Open-ended
`why`/`how`/`what` prompts are Qlob's configured engagement goal, not a universal engine rule.

Keep `RUNWAY_AGENT_RUNTIME=mock` only for the deterministic offline fixture workflow. An explicit
`openai` adapter remains available for development, but it is never selected or used as a fallback
from the configured Codex runtime.

## Discovery

The fixture provider is the default. Manual URLs are downloaded only when explicitly supplied.
The headed browser provider is experimental and disabled until
`RUNWAY_ENABLE_BROWSER_SEARCH=true`; start it explicitly with:

```powershell
.\.venv\Scripts\runway.exe discover images --days 10 --provider browser --live
```

It preserves page/image URLs and stops on challenges. It contains no stealth, CAPTCHA-solving,
proxy rotation, or identity-evasion behavior. Source metadata remains available in the local
  archive but is not part of the fast Runway decision path.

## Guarded YouTube scheduling

The dedicated publisher profile is separate from the capture/discovery profiles:

```powershell
.\.venv\Scripts\runway.exe publisher login
.\.venv\Scripts\runway.exe publisher status
```

`publisher login` opens ordinary installed Google Chrome—not Playwright's
automation browser—with an isolated RunWay profile. Sign in manually with the
Google account that has Qlob Editor access, confirm the Posts page is visible,
close that Chrome window, and then press Enter in the terminal. Guarded
publishing later reopens the saved profile through installed Chrome. Never
disable Google account protections or copy cookies into RunWay.

With `RUNWAY_PUBLISHING_ENABLED=true`, every click on `Accept` is an explicit scheduling
instruction for that exact image and caption. RunWay assigns the next free daily slot, advances the
Runway immediately, and processes the persisted outbox serially. Confirmed Lineup edits, moves,
swaps, and removals are applied to YouTube first and saved locally only after verification. A
session or YouTube error
pauses the outbox. Once a Schedule click may have happened, an inconclusive result enters
`publish_unverified` and can only be verified—not automatically resubmitted.

## Known limitations

- YouTube may expose only relative publication text; RunWay records `relative` precision and does
  not invent exact dates.
- YouTube DOM capture is inherently fragile. Selectors are versioned and failures produce local
  snapshots, but a layout change can require an adapter update.
- Browser discovery quality varies by provider; every image/caption pair remains a human editorial
  decision.
- Deterministic local representations are reproducible baselines, not evaluated neural
  vision-language embeddings.
- The replacement holdout contains only two deterministic policy-proxy cases. It improved from
  0/2 to 2/2, but the uncertainty intervals overlap and blind creator preference remains
  unmeasured.
- Real image generation is architecturally bounded but not enabled; only a lineage-preserving mock
  provider is tested.
- The real capture and a bounded six-candidate browser-discovery pass are verified; wider
  production searches can still encounter provider challenges or layout changes.
- Source and rights metadata remain stored for reference but do not block the Runway feed.
- On the complete profile-v4 holdout, exact historical image-to-caption recovery measured 3/140
  and Qlob-caption ranking measured 35/140. Generated captions therefore remain suggestions
  requiring human judgment.
- The guarded scheduler is implemented and offline-tested, but a real Schedule click has not been
  included in automated QA. One separately authorized Qlob acceptance pass remains.
- The official YouTube Data API does not expose a Community-post resource, so
  Community scheduling cannot be replaced with an OAuth API call. If Google
  refuses even the ordinary-Chrome isolated profile, keep publishing disabled
  and schedule the prepared image/caption manually in normal YouTube.

See `docs/COMPLETION_GUIDE.md` for the remaining path to production,
`docs/OPERATIONS.md` for routine commands, `docs/BUILD_REPORT.md` for the fixture proof, and
`docs/QLOB_PRODUCTION_VALIDATION.md` for the real-data pass.
