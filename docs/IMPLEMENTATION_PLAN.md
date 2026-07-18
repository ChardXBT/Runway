# Implementation plan

This plan records the original milestone structure. The approval-queue portion was superseded on
2026-07-17 by the continuous editorial conveyor described in `docs/PRODUCT.md`: no fixed scheduling
horizon, at most one RunWay post per day, automatic feedback, and accept-to-schedule publishing.

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

## Milestone 4 — editorial conveyor and publishing

- Generate review options independently of schedule slots, then reserve the first open 10:00 AM
  Toronto date only when the operator approves.
- Present one image/caption decision at a time with inline editing, reject, image replacement,
  accept-and-schedule, immediate next-option loading, and automatic learning signals.
- Enforce at most one RunWay-generated post per local day with no fixed horizon.
- Move approved payloads through `InternalPublisher` into a persisted serial outbox and guarded
  visible-browser Qlob publisher.

## Completion proof

- Run initialization, fixture capture/resume/idempotency, verification, analysis, profile build and
  evaluation, fixture discovery, option generation, edit/reject/replace/approve/internal schedule,
  uncapped daily-slot allocation, outbox recovery, service restart/persistence checks, backend
  tests/lint/types, and frontend tests/lint/build.
- Record exact outcomes and honest external-adapter limitations in `docs/BUILD_REPORT.md`.
- Commit only the RunWay repository scope, create `ChardXBT/RunWay` as private, and push after every
  feasible check passes, as explicitly requested by the user.
