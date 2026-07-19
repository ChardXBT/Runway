# Runway intelligence data model

This document describes the canonical intelligence-data flywheel introduced by
Alembic revision `0008_intelligence_data_flywheel`. It supplements
[`DATA_MODEL.md`](DATA_MODEL.md) and the committed structural contract at
[`schema/intelligence-data-flywheel.json`](schema/intelligence-data-flywheel.json).

Runway still has one production intelligence path. The lifecycle records below
version and audit that path; they do not create a second engine or a user-facing
model selector.

## Core invariants

- Every intelligence record is channel-scoped.
- Raw capture evidence and raw model outputs are preserved.
- Corrections, feature snapshots, labels, representations, datasets, models,
  activations, and study responses are append-only or explicitly versioned.
- One and only one validated representation set may be active for a given
  `(channel, scope, purpose)`.
- At most one preference model may be active for a given `(channel, target)`.
- Active records are resolved through explicit lifecycle declarations, never
  by choosing the newest compatible-looking row.
- Historical feature snapshots are immutable. Current feature code may not
  reinterpret a past decision.
- Caption, image, and image-caption pairing feedback are separate targets.
- Human, synthetic, policy, automated, and audience-derived evidence remain
  distinguishable.
- The intelligence agent capability registry cannot publish, schedule, edit,
  or remove YouTube content.
- Optional providers cannot download weights, silently fall back, or activate
  themselves.

## Relationship map

```text
channels
  |
  +-- representation_sets --< representation_set_items >-- representation_records
  |          |
  |          +-- intelligence_activations
  |
  +-- intelligence_agent_runs --< intelligence_agent_steps
  |
  +-- proposals --< proposal_events
  |       |              |
  |       |              +-- derived pairwise_preferences
  |       |              +-- derived feedback_signals
  |       |
  |       +-- caption_exposures
  |       +-- caption_feedback (legacy evidence, reconciled but preserved)
  |
  +-- caption_slates --< caption_candidate_records
  |                         |
  |                         +-- parent candidate for human edits
  |                         +-- immutable feature snapshot
  |                         +-- exact semantic representation
  |
  +-- preference_datasets --< preference_dataset_items
  |             |
  |             +-- preference_model_versions -- intelligence_activations
  |
  +-- annotation_refresh_runs --< annotation_refresh_items
  |
  +-- blind_studies --< blind_study_cases --< blind_study_responses
  |                                                |
  |                                                +-- pairwise_preferences
  |
  +-- active_learning_batches --< active_learning_selections
```

## Schema contract

The committed schema snapshot contains normalized table, column, type,
nullability, default, primary-key, foreign-key, unique-constraint, and index
metadata. Its fingerprint is:

```text
503e7471206e1465c01ad2bbea9f7a6ee3aa9b0446c7aee6f0e8b4ca4fa4a06d
```

`runway database schema-verify` structurally compares the live database with
that snapshot. A matching hash alone is not treated as sufficient: the command
also reports missing, unexpected, and changed structures.

The migration creates its new structures explicitly. It does not call mutable
ORM metadata to construct them. Migration tests prove that a clean install and
an upgrade from `0007_canonical_intelligence` converge to the same normalized
schema.

## Representation lifecycle

### `representation_sets`

One row identifies an immutable plan for a channel, scope, purpose, modality,
provider, model, model version, and provider configuration hash. `plan_hash`
includes the ordered identities and source-content hashes of every expected
entity.

Important states are:

```text
planned -> backfilling -> backfilled -> ready -> active
                                      \-> failed
active -> superseded
active -> rolled_back
```

Only `ready` sets with exact complete coverage, no failed or stale item, valid
vectors, and passing activation gates may become active.

### `representation_set_items`

Each item is a checkpoint for one exact `(entity_type, entity_id, field,
source_content_hash)` identity. It links to one `representation_record` after a
successful bounded backfill. Attempts and failures are retained, making the
backfill resumable and diagnosable.

### `representation_records`

The canonical representation store remains content- and configuration-addressed.
Set items add lifecycle membership without replacing exact read-through cache
semantics. A cache hit requires the exact channel, entity, field, purpose,
provider, model, model version, source hash, and configuration hash.

Normalized vectors must contain finite values and have an L2 norm within 0.01
of 1.0. Vector count, dimensions, dtype, and normalization contracts must be
consistent across an activated set.

### `intelligence_activations`

Activation and rollback append an audit record containing the resource type,
target, prior and new resource IDs, reason, and explicit gate results. Candidate
artifacts are never deleted during supersession or rollback.

## Agent-run provenance

### `intelligence_agent_runs`

A run stores its stable run key, channel, capability, provider/model/prompt
identity, typed input and output envelopes, budget, measured usage,
configuration hash, status, attempt count, timestamps, and terminal error or
abstention reason.

### `intelligence_agent_steps`

Steps record deterministic sequence and bounded attempts, typed input/output,
budget and usage snapshots, timeout, linked artifact identity, and failure
classification. A repeated completed run key returns the persisted result; an
in-flight duplicate fails rather than starting hidden work.

The capability registry contains historical annotation, candidate analysis,
retrieval planning, caption generation, caption verification, and deterministic
mock image generation. Publishing capabilities are rejected by name and are not
registered.

## Decision and learning evidence

### `proposal_events`

Proposal events remain the append-only authority for creator actions. Derived
records link to the source event and use deterministic idempotency keys.

### `caption_candidate_records`

Candidate records now include origin, parent-candidate lineage,
source-proposal-event identity, deterministic derivation key, immutable feature
snapshot and hash, taxonomy/verifier identity, exact representation link, and
ranker model identity.

A creator edit creates a new `human_edit` candidate linked to its parent. It is
reverified against the original editorial brief, receives a fresh immutable
feature snapshot, and receives its own deterministic text representation in the
same transaction. It does not inherit the parent's verification result.

### `pairwise_preferences`

Pairwise records identify a separate target (`caption`, `image`, or `pairing`),
preferred and dispreferred candidate/text identities, source event/exposure/
study response, label source, derivation version, immutable preferred and
dispreferred features, context, feature schema, group key, split, and all
relevant model/retrieval/profile identities.

The unique idempotency key prevents replay from duplicating evidence. Selecting
an alternative and then accepting it does not create a second equivalent pair.

### `feedback_signals`

Normalized feedback is the canonical read path. Signals preserve target,
polarity, strength, reason, label source, and source event or legacy-feedback
identity. Legacy `caption_feedback` rows remain immutable evidence and are
reconciled idempotently into target-specific normalized signals.

## Preference datasets and models

### `preference_datasets` and `preference_dataset_items`

A dataset freezes one target, feature schema, taxonomy version, split seed,
configuration, content hash, row count, and split counts. Dataset items store
the exact preferred/dispreferred feature snapshots, strength, group key, and
split.

Splits are deterministic and group-protected so related proposal/image examples
cannot leak across train, validation, and test.

### `preference_model_versions`

Each model version stores its target, algorithm, frozen dataset, parent model,
feature identity, saved parameters, training configuration, metrics,
calibration report, label threshold, configuration hash, artifact hash, state,
and activation timestamps.

Training is an explicit job. Production scoring only loads persisted parameters;
it never refits in a request path. The current deterministic algorithm is
pairwise logistic ranking. A probability is explicitly reported as
uncalibrated unless independent held-out evidence supports calibration.

No production model is activated merely because a record exists. Activation
requires threshold-qualified genuine evidence, compatible current feature
schema, an immutable matching dataset, complete metrics, finite parameters,
artifact integrity, channel isolation, and explicit gates.

## Annotation refresh

`annotation_refresh_runs` freeze the requested annotation and prompt versions,
provider/model identity, exact planned coverage, status, checkpoint counts, and
validation result. `annotation_refresh_items` preserve the expected source hash,
attempts, produced annotation/model-run identity, correction compatibility, and
error state.

Refreshes add a new annotation version. They never overwrite old raw output.
Real Codex usage requires an explicit operator flag; fixture tests do not make
model calls.

## Blind studies and active learning

`blind_studies`, `blind_study_cases`, and `blind_study_responses` preserve the
preregistered target, seed, case order, group/split protection, hidden origin
labels, genuine reviewer identity, decision time, verdicts, edits, and reason
codes. Review exports omit hidden labels and provider/model origin.

`active_learning_batches` and `active_learning_selections` persist deterministic
uncertainty-, disagreement-, information-, and diversity-aware review queues.
Selections are excluded from the final holdout and retain group/split identity.

## Image-generation lineage

`image_generation_runs.agent_run_id` links generation to its typed harness run.
`generated_asset_lineage.candidate_image_id` and `review_status` prove that a
generated asset re-entered the ordinary candidate review and safeguard path.
Only the deterministic mock provider is enabled by default.

## Label provenance

The following sources must never be collapsed:

- `human`: direct creator or reviewer action;
- `synthetic`: fixture or explicitly generated test label;
- `policy`: deterministic rule outcome;
- `automated`: model/verifier-derived signal;
- `audience`: future measured platform-performance outcome.

Blind-study imports create `human` evidence only from an explicit imported
review response. Runway never infers or fabricates creator labels.

## Current Qlob production state

After the 2026-07-19 migration and local backfill:

- text set `4`, image set `2`, and multimodal set `5` are active;
- active coverage is `698/698`, `716/716`, and `698/698`;
- text and multimodal use deterministic version `2`; image uses version `3`;
- superseded text set `1` and multimodal set `3` remain for rollback/audit;
- 9 legacy feedback rows map to 27 normalized signals, 9 per target;
- no preference dataset or model is active because genuine pairwise labels are
  not yet sufficient;
- no annotation refresh, blind study, or active-learning batch has been
  presented as completed;
- optional trained providers remain uninstalled, unvalidated, and inactive.

See [`INTELLIGENCE_OPERATIONS.md`](INTELLIGENCE_OPERATIONS.md) for backup,
migration, activation, rollback, and doctor procedures.
