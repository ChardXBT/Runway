# Runway Intelligence Data Flywheel

This is the persistent implementation ledger for the canonical intelligence-data
flywheel milestone. It is updated as work is implemented and validated. Status values
are limited to `not_started`, `in_progress`, `blocked`, `implemented`, `validated`,
`deferred`, and `rejected`.

## Milestone identity

- Milestone: `intelligence-data-flywheel`
- Starting commit: `ff70f0ee5bb659554a4e6f2be7a5f32afefd5efb`
- Working branch: `main`
- Repository: private `ChardXBT/Runway`
- Audit date: 2026-07-19
- Intended migration head: `0008_intelligence_data_flywheel`
- Current canonical migration head: `0008_intelligence_data_flywheel`
- Live publishing during implementation and validation: forbidden

## Current-state audit

Status: `validated`

The findings immediately below preserve the starting-state audit at commit
`ff70f0ee5bb659554a4e6f2be7a5f32afefd5efb`. The post-migration state is recorded
after the pre-migration table and in Measured outcomes.

### Repository

- The worktree started clean and synchronized with `origin/main`.
- The 0007 canonical engine has one retrieval, caption, feedback, evaluation, and
  provider path. No alternate production engine will be introduced.
- `RepresentationRecord` has content/configuration identity, but no representation-set
  membership or unambiguous active-set resolver.
- Retrieval recomputes deterministic text vectors and performs row-level representation
  lookups. It does not expose cache hit/miss/stale diagnostics.
- Caption preference scoring currently refits a small pairwise ranker during request
  scoring. There is no persisted dataset, model version, activation, or rollback.
- Caption exposures are mutable. Proposal events are append-only, but derived preference
  and normalized-feedback rows do not yet have source-event identity or deterministic
  derivation keys.
- Human caption edits are stored as replacement text, not as first-class candidates.
- Legacy `caption_feedback` and normalized `feedback_signals` can diverge.
- Historical annotation runs have model-run provenance, but no explicit refresh plan,
  checkpoint item lifecycle, validation gate, or activation record.
- Evaluation artifacts are deterministic and preserve a frozen baseline, but model and
  representation activation are not connected to a common atomic lifecycle.
- The agent runtime is typed at its task boundaries, but has no persisted parent run,
  persisted step graph, capability registry, or per-run budget enforcement.

### Production data

Read-only audit of `data/qlob-production/runway.db`:

| Check | Result |
| --- | ---: |
| SQLite integrity | `ok` |
| Foreign-key violations | `0` |
| Migration | `0007_canonical_intelligence` |
| Tables | `33` |
| Channels | `1` |
| Captured/raw posts | `771` |
| Canonical posts | `771` |
| Training-eligible posts | `698` |
| Media assets | `779` |
| Post annotations | `698` |
| Reviewed annotations | `10` |
| Annotation corrections | `7` |
| Candidate images | `76` |
| Proposals | `7` |
| Proposal events | `40` |
| Legacy caption feedback rows | `9` |
| Representation records | `0` |
| Retrieval runs/evidence | `0 / 0` |
| Caption slates/candidate records | `0 / 0` |
| Caption exposures/pairwise preferences | `0 / 0` |
| Normalized feedback signals | `0` |
| Intelligence experiments | `0` |

Production annotations are currently `historical-annotation-v2`: 688 unreviewed and
10 reviewed. A full v3 refresh would consume Codex allowance and is therefore not part
of unattended validation. The refresh lifecycle and a resumable plan will be delivered;
execution remains an explicit operator action.

The checked-in `.env` points to the production data directory and enables publishing.
All implementation, migration, backfill, doctor, and validation commands must therefore
set `RUNWAY_PUBLISHING_ENABLED=false` explicitly. No publisher command or approval path
may be invoked during this milestone.

Post-migration production verification:

| Check | Result |
| --- | ---: |
| SQLite integrity | `ok` |
| Foreign-key violations | `0` |
| Migration | `0008_intelligence_data_flywheel` |
| Tables | `48` |
| Schema fingerprint | `503e7471206e1465c01ad2bbea9f7a6ee3aa9b0446c7aee6f0e8b4ca4fa4a06d` |
| Preserved channels/posts/media | `1 / 771 / 779` |
| Preserved annotations/corrections | `698 / 7` |
| Preserved candidates/proposals | `76 / 7` |
| Preserved legacy feedback | `9` |
| Normalized feedback | `27` |
| Representation sets/items/records | `5 / 3,508 / 3,513` |
| Active text/image/multimodal coverage | `698 / 716 / 698` |
| Retrieval runs/evidence | `4 / 2,010` |
| Doctor findings | `0 critical / 1 warning / 2 information` |

## Verified assumptions

| Assumption | Status | Finding |
| --- | --- | --- |
| Row-level active representations are ambiguous | `validated` | No set identity or unique active-set scope exists. |
| Representation reads can silently cross model/config choices | `validated` | `latest()` filters purpose/entity only and orders by creation time. |
| Historical text representations are recomputed in retrieval | `validated` | Text vectors are built and persisted inside each historical-pool request. |
| Caption preference scoring refits in production requests | `validated` | Pairwise weights are fit in `score()` when the label threshold is met. |
| Decision-derived learning rows are replay-safe | `rejected` | There is no source-event or derivation idempotency key. |
| Human edits are first-class candidates | `rejected` | They remain replacement text attached to an existing candidate/exposure. |
| Normalized feedback is canonical | `rejected` | Caption context still reads legacy feedback directly. |
| Production has enough current learning evidence to train | `rejected` | Current pairwise and normalized feedback counts are both zero. |
| Production can be representation-backfilled offline | `validated` | Deterministic local providers require no model download or paid call. |
| Existing publishing code must be part of the agent harness | `rejected` | Publishing is intentionally excluded from intelligence capabilities. |

## Invariant checklist

These invariants are release blockers.

- [x] Migration is additive, rollback-aware, and preserves all 0007 data.
- [x] Clean migration to head and 0007-to-head upgrade have identical normalized schemas.
- [x] A deterministic schema fingerprint is committed and checked in tests.
- [x] A representation set names an exact, immutable entity/content/configuration plan.
- [x] A partial or invalid representation set can never become active.
- [x] At most one active representation set exists for a channel/scope/purpose.
- [x] Active-set resolution is explicit; it never selects the newest compatible-looking row.
- [x] Exact cache hits do not recompute; stale content/configuration produces a miss.
- [x] Backfills are bounded, checkpointed, resumable, idempotent, and channel-isolated.
- [x] Agent runs and steps persist typed inputs, outputs, budgets, attempts, and failures.
- [x] The intelligence capability registry contains no publishing capability.
- [x] Provider fallback is explicit and disabled by default; paid fallback remains impossible.
- [x] Proposal events remain append-only and are the authority for decision derivation.
- [x] Replaying a decision derivation cannot duplicate preference or feedback evidence.
- [x] Selecting then accepting the same caption does not create duplicate equivalent pairs.
- [x] A human edit creates a first-class candidate linked to its parent and source event.
- [x] Feature snapshots are immutable, versioned, and created at decision time.
- [x] Dataset construction is deterministic and prevents proposal/image-group leakage.
- [x] Preference models train only in explicit jobs and score from persisted parameters.
- [x] Caption, image, and pairing preference targets remain separate.
- [x] Calibration is reported only when held-out evidence is sufficient.
- [x] Normalized feedback is the canonical read path after reconciliation.
- [x] Annotation refresh plans never overwrite old annotation versions.
- [x] No unattended task invokes Codex, downloads a model, calls a paid API, or uses a GPU.
- [x] Provider adapters are lazy, optional, diagnosable, and never auto-activate.
- [x] Retrieval records active representation-set identities and cache diagnostics.
- [x] Blind-study exports never include hidden labels and imports never invent creator labels.
- [x] Active-learning selections are reproducible and preserve split protection.
- [x] Intelligence doctor returns non-zero for every critical invariant failure.
- [x] Activation and rollback are transactional and leave one canonical production path.
- [x] No live YouTube mutation occurs in tests, backfills, migrations, or evaluations.

## Phase ledger

| Phase | Scope | Status | Validation evidence |
| ---: | --- | --- | --- |
| 1 | Repository and production audit | `validated` | Starting commit, source behavior, production counts, backup, hashes, WAL, integrity, and final counts recorded. |
| 2 | Deterministic schema contract | `validated` | Full normalized snapshot and structural clean-vs-upgraded tests pass; production fingerprint matches. |
| 3 | Representation-set lifecycle | `validated` | Plan, checkpoint, validation, activation, supersession, rollback, failure, and isolation tests pass. |
| 4 | Read-through representation caching | `validated` | Exact/active/get-or-create/stale APIs and diagnostics pass; repeated production retrieval recomputed zero records. |
| 5 | Typed intelligence-agent harness | `validated` | Runs/steps, typed envelopes, budgets, retries, timeout, failure, abstention, and forbidden publishing are persisted/tested. |
| 6 | Append-only decision provenance | `validated` | Source events and deterministic idempotency keys prevent replay and selected-then-accepted duplication. |
| 7 | Human edits as candidates | `validated` | Edits create parent-linked, independently verified candidates with snapshots and exact representations. |
| 8 | Immutable preference snapshots | `validated` | Versioned snapshots and deterministic group-protected datasets pass fixture tests. |
| 9 | Persisted preference-model lifecycle | `validated` | Target-separated explicit training/scoring/activation/rollback passes fixtures; production activation is blocked by real-label scarcity. |
| 10 | Canonical normalized feedback | `validated` | Production reconciliation created 27 target signals from 9 preserved rows; rerun created zero and verification passes. |
| 11 | Annotation refresh lifecycle | `validated` | Resumable/versioned tooling and correction compatibility pass fixtures; real Codex refresh is `deferred`. |
| 12 | Full representation backfill | `validated` | Active production coverage is text 698/698, image 716/716, multimodal 698/698 with no active invalid vector. |
| 13 | Provider research and adapters | `validated` | Official-source matrix and lazy local-only Sentence Transformers/SigLIP2 adapters pass no-download diagnostics; trained evaluation is `deferred`. |
| 14 | Retrieval excellence | `validated` | Retrieval records active sets and cache diagnostics; production repeated run had 0 misses/recomputations. |
| 15 | Reference-image-plus-text retrieval | `validated` | Canonical composed baseline uses active sets, persists provenance, and passes multi-reference/policy fixtures; learned composed retrieval is `deferred`. |
| 16 | Caption intelligence orchestration | `validated` | Canonical caption generation now runs through persisted typed harness and saved-model-only scoring. |
| 17 | Image generation/editing harness | `validated` | Mock generation links agent run, complete lineage, eligibility, safeguards, and normal review re-entry; real providers remain disabled. |
| 18 | Blind creator study | `validated` | Preregister/export/import/report tooling enforces blinding and 50-case exports; genuine production review remains `blocked`. |
| 19 | Active learning | `validated` | Persisted deterministic uncertainty/disagreement/diversity selection and split protection pass tests. |
| 20 | Intelligence database doctor | `validated` | Human/JSON diagnostics and critical exit pass tests; production reports zero critical. |
| 21 | Evaluation and activation | `validated` | Hard gates, artifact integrity, transactional activation, rollback, and production deterministic set activation are proven. |

## Planned migration contract

Migration `0008_intelligence_data_flywheel` will be explicit and additive. It will not
construct new tables from mutable ORM metadata. Planned structures:

- representation sets, immutable plan items, and activation history;
- representation-set membership on representation records;
- persisted intelligence agent runs and steps;
- deterministic decision derivations and source-event keys;
- first-class human-edit candidate lineage and feature snapshots;
- preference datasets, dataset items, model versions, and activation history;
- source-linked normalized feedback reconciliation;
- annotation refresh runs and items;
- blind-study cases/responses and active-learning selections.

Every new table must have a concrete service and test before it remains in the final
schema. Unused speculative tables will be removed before migration freeze.

## Experiment ledger

| Experiment | Status | Dataset/split | Decision |
| --- | --- | --- | --- |
| Frozen 0007 canonical baseline | `validated` | Existing development/tuning/locked holdout artifacts | Preserve unchanged. |
| Deterministic representation baseline | `validated` | Full eligible Qlob history plus fixtures | Active production sets 4/2/5 have complete coverage and zero active invalid vectors. |
| Optional CPU text adapter | `deferred` | Frozen retrieval/caption fixtures | Adapter implemented; dependency/weights absent; no download, evaluation, or activation. |
| Optional visual/multimodal adapter | `deferred` | Frozen retrieval/pairing fixtures | Adapter implemented; dependency/weights absent; no download, evaluation, or activation. |
| Persisted caption preference model | `blocked` | Production pairwise labels | Zero current labels; lifecycle/tests can be validated with fixtures. |
| Persisted image preference model | `blocked` | Production image labels | No normalized production image labels yet. |
| Persisted pairing preference model | `blocked` | Production pairing labels | No normalized production pairing labels yet. |
| Creator blind study | `blocked` | At least 50 Qlob-representative cases | Tooling can be completed; creator judgments require the user. |

`blocked` here means evidence collection—not implementation—is externally dependent.
The milestone is not permitted to manufacture labels or claim measured quality gains.

## Production migration and backfill runbook

Status: `validated`

1. `validated`: stopped the local API/web writers and forced publishing disabled.
2. `validated`: used SQLite online backup semantics to create a timestamped pre-0008 backup.
3. `validated`: recorded integrity, foreign-key checks, migration version, normalized schema
   fingerprint, row counts, and backup hash.
4. `validated`: upgraded an exact backup copy first; schema drift in historical
   server defaults was found and fixed before production.
5. `validated`: upgraded production to 0008 with exact schema convergence.
6. `validated`: reconciled legacy feedback idempotently.
7. `validated`: planned deterministic text, image, and multimodal representation sets from exact
   eligible entity IDs and source hashes.
8. `validated`: backfilled in bounded resumable batches and validated every set.
9. `validated`: activated only complete valid sets transactionally.
10. `validated`: ran the full doctor and repeated retrieval checks with publishing disabled.
11. `validated`: preserved the immutable pre-migration backup and recorded the final audit.

## Deferred and rejected work

- `deferred`: Running a full v3 Qlob annotation refresh. The lifecycle is in scope, but
  real execution requires explicit Codex allowance and operator approval.
- `deferred`: Training or activating production preference models until minimum real,
  target-specific label thresholds are met.
- `deferred`: Recording creator blind-study outcomes until the creator completes the
  blinded review.
- `deferred`: Downloading optional neural model weights or installing large ML stacks.
- `rejected`: Silent fallback from local/Codex operation to a paid API.
- `rejected`: Online refitting of preference weights in a request path.
- `rejected`: Auto-activation based only on a newer version or completed job.
- `rejected`: Synthetic or inferred creator labels presented as human evidence.
- `rejected`: Any agent capability that can publish, schedule, edit, or remove YouTube
  content.

## Measured outcomes

- Clean and upgraded databases converge to the same 48-table structural contract
  and fingerprint.
- The exact pre-migration backup is 27,758,592 bytes, passes integrity and foreign
  keys, and has SHA-256
  `9cafe2da9ea937d6b0f5e9dd4509b89fa85d77c6b06c5ea1e47bcd43f0b91d15`.
- All source evidence counts were preserved: 1 channel, 771 posts, 779 media,
  698 annotations, 7 corrections, 76 candidates, 7 proposals, and 9 legacy
  feedback rows.
- Reconciliation converted 9 legacy rows into 27 separate normalized signals:
  9 caption, 9 image, and 9 pairing. A second pass created zero.
- Active deterministic coverage is 698/698 text, 716/716 image, and 698/698
  multimodal. All active vectors pass finite/count/norm/contract validation.
- A tokenless-caption defect that produced three zero-norm text-v1 vectors was
  detected by the production doctor, fixed in deterministic text v2 and
  multimodal v2, and safely superseded. Invalid v1 rows remain inactive evidence.
- The repeated production retrieval run resolved exact sets 4/2/5 and reported
  1,952 active hits, 4 exact hits, 0 misses, 0 recomputations, and 0 stale misses.
- The final production doctor reports 0 critical, 1 warning, and 2 informational
  findings. The warning is limited to the three preserved inactive v1 vectors.
- Schema/model/representation lifecycle, replay safety, channel isolation,
  no-download, no-paid-fallback, and no-publishing claims are test-backed.
- No creator-preference, learned semantic-quality, or calibration improvement is
  claimed: production has zero pairwise labels and no active preference model.

## Blockers

- Production label scarcity blocks honest preference-model activation.
- Creator participation blocks a completed blind preference study.
- Optional neural-provider evaluation requires separately installed dependencies and
  locally available weights; unattended installation/download is prohibited.
- A real annotation refresh requires explicit Codex usage and remains outside this
  non-mutating validation pass.
