# Implementation plan

This plan translates the supplied single-pass specification into checkpointed implementation work.
Each checkpoint keeps the local application runnable and is followed by targeted tests before the
next milestone begins.

## Milestone 0 — foundation

- Create the Python/Next.js monorepo, typed settings, structured logging, local data layout, and
  safe environment template.
- Define SQLAlchemy records, Alembic migrations, SQLite WAL configuration, domain enums, and the
  proposal state machine.
- Add the Typer command tree, FastAPI health/settings shell, accessible Next.js application shell,
  CI, tests, and developer scripts.

## Milestone 1 — capture and catalogue

- Implement a versioned DOM adapter, deterministic sanitized fixtures, fixture capture, resumable
  checkpoints, idempotent post/media upserts, and content-addressed media storage.
- Implement explicit, headed managed-browser and CDP paths with read-only safety gates, challenge
  stops, conservative scrolling, diagnostic bundles, and no recurring crawl.
- Add catalogue CLI/API/UI views and JSON/Markdown verification reports.

## Milestone 2 — intelligence

- Extract deterministic caption/image features, local embeddings, pairwise similarity, typed mock
  and OpenAI runtimes, model-run auditing, and annotation correction overlays.
- Build versioned, reproducible style profiles, bounded local retrieval, deterministic holdout
  evaluation, reports, and profile/catalogue UI surfaces.

## Milestone 3 — discovery and captions

- Add fixture, manual URL, feature-gated headed-browser, and optional API search providers.
- Preserve provenance; inspect quality; create original/square-preview media; detect exact,
  transformed, source, visual, and semantic duplication; apply explainable weighted ranking.
- Generate and validate three retrieval-grounded captions while rejecting near-copy captions and
  recording complete model/audit history.

## Milestone 4 — approval and queue

- Generate one restart-safe proposal per local day for ten days at 10:00 America/Toronto with
  backups and DST-safe timestamps.
- Implement review, edit, alternative selection, regeneration, replacement, rejection, approval,
  rescheduling, block, metadata correction, calendar, settings, and activity views/actions.
- Implement `InternalPublisher` with a strict approval boundary and document the future headed
  browser publisher without implementing live publishing.

## Completion proof

- Run initialization, fixture capture/resume/idempotency, verification, analysis, profile build and
  evaluation, fixture discovery, ten-day generation, edit/reject/replace/approve/internal schedule,
  service restart/persistence checks, backend tests/lint/types, and frontend tests/lint/build.
- Record exact outcomes and honest external-adapter limitations in `docs/BUILD_REPORT.md`.
- Commit only the LeeWay repository scope, create `ChardXBT/LeeWay` as private, and push after every
  feasible check passes, as explicitly requested by the user.
