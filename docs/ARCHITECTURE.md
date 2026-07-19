# Architecture

RunWay has one canonical production intelligence path. The CLI, FastAPI routes,
and Next.js UI call service-layer operations; services own capture, catalogue,
analysis, retrieval, captions, generation, proposals, feedback, scheduling, and
audit events. SQLAlchemy repositories and lifecycle services own SQLite
transactions. Typed provider/runtime protocols isolate external integrations.

## Canonical intelligence flow

```text
channel history + media + corrections + policy + recent state
                              |
                              v
                 canonical channel evidence
                              |
                              v
        explicit active text/image/multimodal sets
                              |
                              v
      persisted hybrid + reference-image/text retrieval
                              |
                              v
                    typed editorial brief
                              |
                              v
        caption/image candidates through safe providers
                              |
                              v
      grounding + policy + rights + duplicate verification
                              |
                              v
       separate image/caption/pairing ranking and slate
                              |
                              v
             creator select/edit/accept/reject
                              |
                              v
      append-only event + normalized target feedback
                              |
                              v
 immutable feature snapshots + pairwise preferences
                              |
                              v
 frozen dataset -> explicit training -> gated activation
                              |
                              +---------- feeds future retrieval/ranking
```

The database is the durable evidence, lineage, training, evaluation, activation,
and rollback layer. It is not a model-weight fine-tuning store. Production
scoring loads a persisted active model when one is qualified; otherwise the
canonical deterministic fallback remains explicit and no request-time fitting
occurs.

## Representation resolution

`representation_sets` freeze the exact expected channel/entity/content/provider
plan. Bounded set items checkpoint backfill work and link to exact
content/configuration-addressed `representation_records`. Validation checks
coverage, identity, files, dimensions, finite values, normalization, and
contract consistency before activation.

Retrieval resolves one explicit active set per `(channel, scope, purpose)`. It
never chooses a row because it is newest. Retrieval runs persist active set IDs,
provider/model/configuration identity, considered and selected evidence, and
cache hit/miss/recomputation/stale diagnostics.

## Typed intelligence harness

The harness persists a parent run and bounded attempts with typed inputs and
outputs, provider/model/prompt identity, budgets, timeouts, usage, artifacts,
errors, and abstention. Missing capabilities fail without fallback. Its registry
contains no publishing or platform-mutation capability.

Optional neural representation adapters are lazy and local-files-only. They do
not download weights, trust remote code, activate themselves, or replace the
deterministic baseline without frozen evaluation and an explicit transactional
activation.

## Decision and learning path

Proposal events are append-only decision authority. Idempotent derivation links
each learning row to its source event. Human caption edits become first-class
verified candidates with parent lineage, immutable feature snapshots, and an
exact text representation.

Caption, image, and pairing labels remain separate. Deterministic,
group-protected datasets freeze decision-time features. Explicit training jobs
persist model parameters, metrics, calibration truth, and artifact hashes.
Activation and rollback append auditable records and preserve superseded
artifacts.

## External platform boundary

The visible YouTube publisher is a separate service boundary. It cannot be
invoked by the intelligence harness, requires the operator's explicit `Accept`
decision, and is feature-gated. Approvals reserve the first open 10:00 AM
Toronto slot with a strict one-RunWay-post-per-day invariant, then enter a
restart-safe serial FIFO outbox.

The API listens only on `127.0.0.1` and serves media from the configured local
data directory. The offline fixture path uses the mock runtime. The real caption
runtime uses the project-local Codex CLI with ChatGPT authentication and no paid
API fallback.

Detailed references:

- [`INTELLIGENCE_DATA_MODEL.md`](INTELLIGENCE_DATA_MODEL.md)
- [`INTELLIGENCE_OPERATIONS.md`](INTELLIGENCE_OPERATIONS.md)
- [`INTELLIGENCE_PROVIDER_MATRIX.md`](INTELLIGENCE_PROVIDER_MATRIX.md)
- [`BLIND_CREATOR_STUDY.md`](BLIND_CREATOR_STUDY.md)
