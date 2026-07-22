# Build status

Last validated: 2026-07-20

## Software build

- [x] Foundation, local API/CLI, SQLite WAL, and Alembic migrations
- [x] Complete read-only Qlob capture and verified local catalogue
- [x] ChatGPT-authenticated Codex runtime with no paid-API fallback
- [x] Historical analysis, retrieval profile, correction overlays, and holdout evaluation
- [x] Bounded image discovery with duplicate, NSFW, provenance, and creator-policy safeguards
- [x] Question-first caption generation grounded in the image and Qlob history
- [x] Automatic append-only learning from edits, approvals, replacements, and rejections
- [x] Explicit `Fewer like this` feedback for repetitive image clusters
- [x] Target-specific caption, image, and pairing readiness with held-out-label isolation
- [x] Continuous one-decision review conveyor with immediate next-option loading
- [x] Uncapped future scheduling at one Runway post per day, 10:00 AM Toronto time
- [x] Local-only approval plus explicit Lineup-to-publisher FIFO outbox
- [x] Visible-browser Qlob publisher with channel/capability validation and duplicate prevention
- [x] Durable passive connection status with a 24-hour stale threshold
- [x] Persisted pause-and-resume behavior when the Google session needs attention
- [x] Human-readable, filterable Activity history with technical records preserved
- [x] Desktop, tablet, and 390px mobile interface QA
- [x] Private GitHub repository with `main` as the working branch

## Quality gate

- Ruff lint: passed
- Ruff format: 140 files passed
- Mypy strict mode: 97 source files passed
- Pytest: 146 passed in 9m41s
- ESLint: passed
- Vitest: 62 passed across 11 frontend test files
- Next.js production build and TypeScript: passed
- Root and web npm audits: zero known vulnerabilities
- Impeccable UI-pattern audit: zero findings
- `git diff --check`: passed; Git reports only its normal CRLF-to-LF notice for `runway.css`

The Python suite emits one dependency-level deprecation warning from FastAPI/Starlette's synchronous
`TestClient`, which recommends the future `httpx2` transport. Replacing it with raw `httpx`
`ASGITransport` was tested and rejected because it does not provide equivalent application
lifespan/background-task handling. No production path uses `TestClient`.

## Real browser validation

An isolated, publishing-disabled dataset was created with 12 historical posts, 23 accepted image
candidates, and 10 generated proposals. Playwright exercised the actual running API and interface:

- double-clicking `Fewer like this`, Accept, Settings save, and swap confirmation produced one
  logical mutation each;
- punctuation edits persisted exactly;
- leaving and returning to Generator preserved the generated queue and current server state;
- an occupied one-day move explained and confirmed a two-post swap;
- Escape closed the dialog and returned focus to its opener;
- an offline Accept preserved input, showed no success, locked uncertain retries, and made no
  schedule mutation;
- invalid timezone input remained visible with an accessible error;
- Activity filtering, Archive history, connector guidance, and target-specific Profile readiness
  rendered from live isolated data;
- 1440px desktop, 768px tablet, and 390px mobile had no horizontal overflow;
- reduced-motion emulation disabled smooth scrolling and reduced transitions to effectively zero.

No ChatGPT usage, live image search, Qlob post, or production YouTube mutation occurred during this
validation.

## Honest limitations

- Captions remain human-reviewed suggestions. Historical holdout performance does not justify
  unattended caption selection.
- Google, YouTube, and image-search markup can change. Browser adapters stop on authentication,
  challenge, channel mismatch, or selector uncertainty instead of bypassing them.
- The exact delegated YouTube role cannot be read through the available API; Runway verifies
  operational channel and composer capabilities instead.
- Scheduled-post edit/remove behavior for the real delegated account remains an explicit external
  acceptance check. Routine tests use a fake adapter and never mutate Qlob.
- Community-post audience analytics are not yet imported, and the local single-user build does not
  provide hosted signup or tenant isolation.

See `docs/CURRENT_LIMITATIONS.md` for the detailed blocker map, `docs/COMPLETION_GUIDE.md` for the
operating workflow, and `docs/OPERATIONS.md` for routine commands.
