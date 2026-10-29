# Build status

Last validated: 2026-10-12 (local Windows run; CI results are on the GitHub Actions page)

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
- [x] Private recovery release tooling with every database-referenced media asset; it refuses
  to run while the repository is public
- [x] Post-push database release sequencing with remote download-and-restore verification
- [x] Public GitHub repository under a proprietary license, with `main` as the working branch
- [x] Bounded, SSRF-resistant image downloader for discovery and capture
- [x] Loopback `Host` and cross-origin request guards on the API and web server
- [x] Scheduled-task watchdog that restarts a crashed component without killing anything

## Quality gate

- Ruff lint and format: passed (180 files)
- Mypy strict mode: passed across 112 source files
- Pytest: 346 passed in 30m39s (Windows, Python 3.13)
- ESLint, TypeScript, and the Next.js 16.4.0 production build: passed
- Vitest: 101 passed across 15 frontend test files
- `pip-audit`: no known vulnerabilities
- `npm audit` (root) and `npm --prefix apps/web audit --omit=dev`: zero vulnerabilities
- Full web audit: clean with no recorded exceptions (the former dev-only `braces` advisory
  was retired after a patched release; see `SECURITY.md`)
- Launcher scenarios on the built stack with an isolated data directory: cold start,
  idempotent rerun, API crash recovery, unrelated process on port 8000, concurrent launches
- `git diff --check`: passed

## Real browser validation (2026-07-26)

An isolated, publishing-disabled restore of the production database and required media was used for
the browser and stress pass. Playwright and the running API exercised the real product interface:

- a double-click on Accept produced one logical decision and exactly one Lineup item;
- punctuation edits persisted exactly;
- an empty Generator began a real refill, kept its running state while navigating away, and showed
  the generated option after returning;
- a cached, already-rendered image correctly unlocked review actions after hydration;
- an occupied one-day move explained and confirmed a two-post swap;
- Escape closed the dialog and returned focus to its opener;
- a 50-item acceptance stress pass kept all 76 resulting Lineup dates unique, survived 20
  occupied-date swaps, produced 50 assisted preflights, blocked a duplicate accept, and retained
  the same invariants after restart;
- three real public-archive discovery passes evaluated 54 candidates and produced six clean,
  varied options while provider failover remained available;
- all seven product sections at 1440px, 768px, and 390px returned HTTP 200 with the intended page,
  no error boundary, console error, page error, horizontal overflow, or off-screen control;
- reduced-motion emulation was enabled throughout the 21-route viewport matrix.

No ChatGPT usage, Qlob post, or production YouTube mutation occurred during this validation.

## Database recovery validation (2026-07-26)

- SQLite online backup restored to a clean temporary directory.
- 52 table counts, migration `0011_runtime_performance`, database SHA-256, archive SHA-256,
  integrity check, and foreign-key check all matched.
- All 1,545 database-referenced media files (246.02 MiB of content) were archived, individually
  hashed, restored, and rechecked.
- The restored database passed the intelligence doctor with 0 critical findings and 0 warnings.
- Three malformed vectors remain intentionally quarantined in immutable inactive sets and are
  reported as information with `active_read_impact: false`.

## Honest limitations

- Captions remain human-reviewed suggestions. More genuine accepts, edits, rejects, image choices,
  and pairing choices are needed before a personalized model can pass the product challenger and
  blind-study gates.
- Google, YouTube, and image-search markup can change. Browser adapters stop on authentication,
  challenge, channel mismatch, or selector uncertainty instead of bypassing them.
- The exact delegated YouTube role cannot be read through the available API; Runway verifies
  operational channel and composer capabilities instead.
- Scheduled-post edit/remove behavior for the real delegated account remains an explicit external
  acceptance check. Routine tests use a fake adapter and never mutate Qlob.
- Community-post audience analytics are not yet imported, and the local single-user build does not
  provide hosted signup or tenant isolation. These are expansion features, not defects in Qlob's
  current local workflow.

See `docs/CURRENT_LIMITATIONS.md` for the detailed blocker map, `docs/COMPLETION_GUIDE.md` for the
operating workflow, and `docs/OPERATIONS.md` for routine commands.
