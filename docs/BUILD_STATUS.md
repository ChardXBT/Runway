# Build status

## Software build

- [x] Foundation, local API/CLI, SQLite WAL, and Alembic migrations
- [x] Complete read-only Qlob capture and verified local catalogue
- [x] ChatGPT-authenticated Codex runtime with no paid-API fallback
- [x] Historical analysis, retrieval profile, correction overlays, and holdout evaluation
- [x] Bounded image discovery with duplicate, watermark, fan-art, and personal-artwork safeguards
- [x] Question-first caption generation grounded in the image and Qlob history
- [x] Automatic append-only learning from edits, approvals, replacements, and rejections
- [x] Continuous one-decision review conveyor with immediate next-option loading
- [x] Uncapped future scheduling at one Runway post per day, 10:00 AM Toronto time
- [x] Approve-to-schedule persisted FIFO outbox
- [x] Visible-browser Qlob publisher with channel/Editor validation and duplicate prevention
- [x] Persisted pause-and-resume behavior when the Google session needs attention
- [x] Desktop and 390px mobile interface QA
- [x] Private GitHub repository with `main` as the working branch

## Quality gate

- Ruff lint and format: passed
- Mypy strict mode: passed
- Pytest: 51 passed
- ESLint: passed
- Vitest: 6 passed across two component files
- Next.js production build and TypeScript: passed
- Root and web npm audits: zero known vulnerabilities

## Honest limitations

- Captions remain human-reviewed suggestions. Historical holdout performance does not justify
  unattended caption selection.
- Google, YouTube, and image-search markup can change. Browser adapters stop on authentication,
  challenge, channel mismatch, or selector uncertainty instead of bypassing them.
- Source metadata and automated warnings do not determine copyright permission. They are retained
  as diagnostic evidence but are no longer an approval-form gate.
- A real Qlob schedule action is an external acceptance test. Routine automated and visual QA never
  clicks `Accept` or YouTube's final Schedule button.

See `docs/COMPLETION_GUIDE.md` and `docs/OPERATIONS.md` for the operating workflow.
