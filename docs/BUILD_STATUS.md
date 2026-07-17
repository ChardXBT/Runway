# Build status

## Software build

- [x] Foundation, local API/CLI, SQLite WAL, and Alembic migrations
- [x] Complete read-only Qlob capture and verified local catalogue
- [x] ChatGPT-authenticated Codex runtime with no paid-API fallback
- [x] Historical image analysis, retrieval profile, correction overlays, and holdout evaluation
- [x] Bounded image discovery with duplicate, watermark, fan-art, personal-artwork, and source safeguards
- [x] Question-first caption generation with deterministic grounded prompts and three structural choices
- [x] Append-only learning records for edits, selections, approvals, preferences, and rejections
- [x] Review UI with image/caption feedback, provenance gate, alternatives, and learning history
- [x] Ten-day queue, approval state machine, and internal scheduling
- [x] Guarded visible-browser YouTube scheduler with a dedicated persistent profile
- [x] Feature gate, channel/Editor validation, one-time token, exact typed phrase, payload hash, screenshots, and post-verification
- [x] Conservative ambiguous-submission recovery that cannot automatically retry
- [x] Desktop and 390px mobile interface QA
- [x] Private GitHub repository with `main` as the working branch

## Quality gate

- Ruff lint and format: passed
- Mypy strict mode: passed for 59 source files
- Pytest: 47 passed
- ESLint: passed
- Vitest: 5 passed across two component files
- Next.js production build and TypeScript: passed
- Root and web npm audits: zero known vulnerabilities

## Honest limitations

- Generated captions remain suggestions. The historical holdout did not demonstrate reliable exact
  caption recovery, so human review stays mandatory.
- Internet-image rights remain `unknown` until a human reviews provenance. Adding a caption does not
  establish permission.
- Google, YouTube, and image-search markup can change. Browser adapters stop on authentication,
  challenge, or selector uncertainty rather than bypassing it.
- The guarded publisher is implemented and fake-adapter tested, but no real YouTube Schedule button
  was clicked during this build. One explicitly authorized controlled schedule is still required to
  accept the current Qlob account/browser environment.

## Remaining deployment acceptance

The software implementation is complete. The only external acceptance item is:

1. Sign the Qlob Editor account into LeeWay's dedicated publisher profile.
2. Temporarily enable `LEWAY_PUBLISHING_ENABLED=true`.
3. Select one approved, provenance-reviewed, internally scheduled proposal.
4. Prepare it, inspect the exact channel/image/caption/time, then separately authorize and type the
   proposal-specific confirmation phrase.
5. Verify the item in Qlob's Scheduled tab, preserve the audit/screenshots, and return the feature
   gate to `false`.

That step creates an external side effect and must not be bundled into routine tests. See
`docs/COMPLETION_GUIDE.md` and `docs/OPERATIONS.md`.
