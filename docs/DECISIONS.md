# Architectural decisions

## One repository

Leeway uses one monorepo for the API, CLI, intelligence pipeline, capture adapter, and review UI.
Separate repositories were rejected because they would duplicate contracts and complicate an
offline, single-user installation.

## SQLite and local files

Canonical metadata lives in SQLite with WAL and migrations; media and raw diagnostic artifacts
live under `data/`. A network database/vector service was rejected because the initial catalogue
is small, local privacy matters, and NumPy brute-force retrieval is adequate.

## Retrieval and a versioned profile

Generation uses calculated statistics, local embeddings, structured annotations, feedback, and a
bounded context package. Immediate fine-tuning was rejected because it is less inspectable, needs
more curated data, and is unnecessary for the current scope.

## User-assisted one-time capture

Historical capture is explicit, read-only, headed, resumable, and uses a dedicated profile. Stored
owner credentials, hidden cookie extraction, stealth, and scheduled re-crawls were rejected for
safety and maintainability.

## Internal publisher only

`InternalPublisher` can move an approved item to `internally_scheduled`; it performs no network
publishing. Live YouTube automation was rejected for this build to preserve the approval boundary.
A future adapter must use a separate Editor (Limited) account/profile and explicit user action.

## Pluggable discovery

Fixture, manual, headed-browser, and optional API providers implement shared contracts. A single
hard-coded scraper was rejected because search surfaces and rights metadata vary and change.

## Deterministic local image features

The first version uses SHA-256, perceptual hashes, crop-resistant descriptors, and local numeric
embeddings. A hosted vector service and opaque model-only ranking were rejected so duplicate and
selection decisions remain reproducible and explainable.

## OpenAI adapter boundary

The real runtime uses the Responses API structured-output parser with Pydantic schemas and an
environment-supplied model. It is never the default, and no model ID is hard-coded. This follows
the current official structured-output guidance while preserving a deterministic mock path.
