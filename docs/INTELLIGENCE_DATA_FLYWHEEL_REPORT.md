# Runway Intelligence Data Flywheel implementation report

Date: 2026-07-19

Starting commit: `ff70f0ee5bb659554a4e6f2be7a5f32afefd5efb`

Branch: `main`

Repository: private `ChardXBT/Runway`

Canonical migration: `0008_intelligence_data_flywheel`

## 1. Architectural assessment

The implementation preserves one canonical Runway intelligence path and turns
its database into an explicit evidence, representation, training, activation,
and rollback layer.

Confirmed starting assumptions:

- representation records supported multiple provider/model/configuration
  identities but had no complete active-set lifecycle;
- retrieval could recompute immutable historical representations and resolve
  rows without a channel-level active-set declaration;
- deterministic text, image, and multimodal providers were reproducible
  baselines rather than trained semantic models;
- caption preference scoring could fit small pairwise weights during request
  scoring;
- exposures existed, but derived decision evidence was not fully source-linked
  and replay-safe;
- human edits were not complete independently verified first-class candidates;
- normalized and legacy feedback could diverge;
- annotation refresh lacked a frozen plan/checkpoint/activation lifecycle;
- there was no persisted parent/step intelligence-agent harness;
- genuine creator preference labels were too sparse for a production model;
- there was no complete intelligence-specific database doctor or structural
  schema fingerprint.

Rejected or revised assumptions:

- production did not have enough labels to train: production had zero pairwise
  labels, so no model was activated;
- no real image-generation provider was suitable or authorized: only the mock
  boundary remains enabled;
- a database platform migration was unnecessary for the local single-user
  deployment; SQLite remains appropriate;
- a new V2 engine, shadow system, or runtime model selector was unnecessary and
  was deliberately not created.

Hidden issues found:

- clean bootstrap and historical upgrades differed in four retained SQLite
  server defaults despite sharing the same logical models;
- three emoji/tokenless captions produced normalized text-v1 vectors with norm
  zero;
- the earlier representation validator checked identity and dimensions but did
  not reject nonfinite, zero-norm, or inconsistent vector contracts before
  activation;
- legacy feedback reconciliation needed canonical target separation and a
  flushed count to prove idempotency;
- selected-then-accepted and human-edit paths needed deterministic derivation
  identities to prevent overweighting.

Architect-initiated changes:

- committed a full structural schema contract, not only a table-name hash;
- normalized historical defaults inside the explicit migration so clean and
  upgraded schemas converge;
- separated invalid active/read-through vectors (critical) from preserved
  invalid inactive vectors (warning);
- versioned deterministic text and multimodal providers to `2` after adding a
  normalized raw-content fallback for tokenless captions;
- required human edits to be reverified against the original editorial brief
  and represented in the same transaction;
- made persisted model activation verify the frozen dataset, current feature
  schema, finite parameter shape, complete metrics, calibration truth, and
  artifact hash.

Deliberately rejected complexity:

- PostgreSQL, a vector database, a hosted control plane, and multi-tenancy;
- permanent old/new engine selection;
- request-time training;
- hidden paid or remote fallback;
- automatic model downloads or GPU use;
- `trust_remote_code=True`;
- a real generation provider without rights/safety/resource authorization;
- fabricated human labels or synthetic creator-study outcomes;
- an unattended Codex annotation refresh;
- neural-provider activation based on a public benchmark alone.

## 2. Exact files changed

Configuration and packaging:

- `.env.example`
- `pyproject.toml`
- `src/runway/config/settings.py`

Migration and database contract:

- `alembic/versions/0008_intelligence_data_flywheel.py`
- `src/runway/db/models.py`
- `src/runway/db/schema_contract.py`
- `docs/schema/intelligence-data-flywheel.json`

Canonical intelligence services:

- `src/runway/intelligence/agent_harness.py`
- `src/runway/intelligence/annotation_refresh.py`
- `src/runway/intelligence/doctor.py`
- `src/runway/intelligence/embeddings.py`
- `src/runway/intelligence/neural_providers.py`
- `src/runway/intelligence/representation_sets.py`
- `src/runway/intelligence/retrieval.py`
- `src/runway/intelligence/studies.py`

Caption, feedback, proposal, and generation integration:

- `src/runway/captions/exposures.py`
- `src/runway/captions/feature_snapshots.py`
- `src/runway/captions/feedback.py`
- `src/runway/captions/preference_models.py`
- `src/runway/captions/preferences.py`
- `src/runway/captions/service.py`
- `src/runway/generation/service.py`
- `src/runway/proposals/service.py`
- `src/runway/cli/main.py`

Documentation:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/BLIND_CREATOR_STUDY.md`
- `docs/DATA_MODEL.md`
- `docs/INTELLIGENCE_DATA_FLYWHEEL.md`
- `docs/INTELLIGENCE_DATA_FLYWHEEL_REPORT.md`
- `docs/INTELLIGENCE_DATA_MODEL.md`
- `docs/INTELLIGENCE_OPERATIONS.md`
- `docs/INTELLIGENCE_PROVIDER_MATRIX.md`
- `docs/OPERATIONS.md`

Updated tests:

- `tests/integration/test_canonical_migration.py`
- `tests/integration/test_caption_learning.py`
- `tests/integration/test_foundation.py`
- `tests/integration/test_generation_lineage.py`
- `tests/integration/test_intelligence_evidence.py`
- `tests/unit/test_intelligence_foundations.py`

New tests:

- `tests/e2e/test_intelligence_flywheel.py`
- `tests/integration/test_agent_harness.py`
- `tests/integration/test_annotation_refresh_lifecycle.py`
- `tests/integration/test_intelligence_doctor.py`
- `tests/integration/test_preference_model_lifecycle.py`
- `tests/integration/test_representation_lifecycle.py`
- `tests/integration/test_schema_contract.py`
- `tests/integration/test_study_active_learning.py`
- `tests/unit/test_intelligence_cli.py`
- `tests/unit/test_neural_providers.py`

Production media, database files, backups, gate artifacts, and browser profiles
remain ignored and are not committed.

## 3. Migration and schema contract

Revision `0008_intelligence_data_flywheel` explicitly creates 15 immediately
used lifecycle tables:

- `representation_sets`;
- `representation_set_items`;
- `intelligence_activations`;
- `intelligence_agent_runs`;
- `intelligence_agent_steps`;
- `preference_datasets`;
- `preference_dataset_items`;
- `preference_model_versions`;
- `annotation_refresh_runs`;
- `annotation_refresh_items`;
- `blind_studies`;
- `blind_study_cases`;
- `blind_study_responses`;
- `active_learning_batches`;
- `active_learning_selections`.

It extends:

- `intelligence_retrieval_runs` with active-set identity and cache diagnostics;
- `caption_candidate_records` with edit lineage, source event, immutable
  features, verifier/taxonomy/model identity, and representation linkage;
- `pairwise_preferences` with target, source event/exposure/study linkage,
  idempotency, immutable features/context, group/split, and model/retrieval
  identity;
- `feedback_signals` with canonical source and derivation identity;
- `image_generation_runs` with agent-run linkage;
- `generated_asset_lineage` with candidate/review re-entry.

Composite lookup, resolution, checkpoint, training, source, split, and
idempotency indexes support every new service path. Foreign keys enforce
channel-adjacent lineage; service and doctor checks enforce cross-table channel
equality where SQLite cannot express it as one constraint.

The full 48-table normalized contract includes columns, types, nullability,
defaults, primary keys, unique constraints, foreign keys, and indexes. Expected
and production fingerprints are both:

```text
503e7471206e1465c01ad2bbea9f7a6ee3aa9b0446c7aee6f0e8b4ca4fa4a06d
```

Clean install and exact `0007` upgrade schemas compare equal. Structural tests
also prove that missing indexes, changed foreign keys, and changed column
attributes are detected.

Production migration result:

- integrity: `ok`;
- foreign-key violations: `0`;
- tables: `48`;
- channels/posts/media preserved: `1 / 771 / 779`;
- annotations/corrections preserved: `698 / 7`;
- candidates/proposals preserved: `76 / 7`;
- legacy feedback preserved: `9`.

The pre-migration backup is:

```text
data/qlob-production/backups/runway-pre-0008-20260719T034751Z.db
```

It is 27,758,592 bytes, has SHA-256
`9cafe2da9ea937d6b0f5e9dd4509b89fa85d77c6b06c5ea1e47bcd43f0b91d15`,
passes integrity, has zero foreign-key violations, and remains at migration
`0007_canonical_intelligence`.

The exact backup was upgraded in an isolated rehearsal first. That rehearsal
exposed historical-default drift; production was not migrated until the
migration normalized it and the fingerprints matched.

## 4. Representation lifecycle

Set model:

- a set freezes channel, scope, purpose, modality, provider, model/version,
  provider configuration hash, ordered expected entities/source hashes, and
  deterministic plan hash;
- set items are bounded checkpoints with attempts, source locator/hash, state,
  error, and exact representation record;
- repeating the same plan is idempotent.

Lifecycle:

- plan: freezes exact expected work without computing or activating;
- backfill: bounded, resumable, retryable, channel-isolated, and inactive;
- validation: requires exact coverage/identity, finite values, matching vector
  count, valid normalization, and one consistent vector contract;
- activation: transactional and audit-recorded;
- supersession: preserves the prior set and records the parent;
- rollback: restores a complete compatible prior set without deletion.

Cache behavior:

- exact reads require channel/entity/field/purpose/provider/model/version/source
  hash/configuration hash;
- an active historical set is resolved by exact `(channel, scope, purpose)`;
- missing or mismatched active-set providers fail closed with no fallback;
- stale source or configuration forces a miss and records diagnostics;
- retrieval aborts if active set identity changes during a run.

Production active sets:

| Modality | Set | Provider/model | Version | Coverage | Invalid active |
| --- | ---: | --- | ---: | ---: | ---: |
| Text | 4 | `runway-local/signed-subword-concepts` | 2 | 698/698 | 0 |
| Image | 2 | `runway-local/image-descriptor` | 3 | 716/716 | 0 |
| Multimodal | 5 | `runway-local/image-text-concatenation` | 2 | 698/698 | 0 |

Text set 1 and multimodal set 3 are superseded. Three zero-norm text-v1 rows
remain preserved in inactive set 1, where they cannot serve active reads.

## 5. Agent harness

The canonical typed harness persists:

- stable run key and deterministic configuration hash;
- channel, capability, provider, model, and prompt identity;
- forbidden-extra typed input/output envelopes;
- max steps, attempts, tokens, wall-clock time, and per-attempt timeout;
- usage, status, attempts, errors, and typed abstention;
- step sequence/attempt identity and linked artifact;
- completed-run replay without repeating provider work.

Retries are bounded. Timeouts and budget exhaustion are terminal. A missing
provider/capability raises a no-fallback error.

Registered capabilities are historical annotation, candidate analysis,
retrieval planning, caption generation, caption verification, and deterministic
mock image generation. Publishing, scheduling, deletion, platform mutation, and
publisher capabilities are forbidden and absent.

Caption generation uses the harness for primary and bounded retry calls and
links completed runs to the caption slate. Mock image generation links its run
to generation and lineage records. Production currently has zero agent runs
because no real production generation/annotation pass was invoked during this
milestone.

## 6. Decision evidence

Caption exposure preserves the displayed ordered slate and decision context.
Proposal events remain append-only action authority.

Derived evidence now stores:

- source proposal event, exposure, and deterministic event key;
- derivation version and unique idempotency key;
- target and label source;
- candidate identities and exact text;
- decision-time feature/context snapshot;
- feature/taxonomy/verifier/profile/representation/retrieval/ranker identity;
- group key and learning split.

Behavior:

- replaying a decision produces no duplicate evidence;
- selecting an alternative and accepting it produces one equivalent preference,
  not two weighted copies;
- acceptance without a prior selection derives the intended evidence;
- rejection derives target-specific negative feedback;
- edit derives the parent-versus-edit preference and separate normalized target
  signals;
- raw events and legacy feedback remain preserved.

## 7. Human-edited candidates

A caption edit now creates a first-class `CaptionCandidateRecord` with:

- `origin=human_edit`;
- parent candidate and source proposal-event linkage;
- deterministic derivation key;
- the original persisted editorial brief;
- fresh grounding and policy verification;
- taxonomy/verifier identity;
- immutable style/novelty/pairing/decision context features and hash;
- its own deterministic text representation;
- pairwise linkage by candidate IDs.

Creation, verification, snapshot, representation persistence, and decision
derivation occur transactionally. The edit never inherits its parent's
verification result. An ineligible edit remains evidence but cannot be treated
as a verified candidate.

## 8. Preference datasets and models

Targets are strictly separate: `caption`, `image`, and `pairing`.

Dataset construction:

- freezes the current feature schema and taxonomy;
- consumes immutable decision-time snapshots;
- records human/synthetic/policy/automated provenance;
- assigns deterministic train/validation/test splits;
- keeps a proposal/image group in one split;
- computes deterministic configuration/content hashes.

The implemented model is deterministic pairwise logistic ranking with saved
feature names and weights. Training occurs only in an explicit job.
Production scoring loads persisted parameters and never refits.

Each model stores dataset, parent model, feature schema, parameters, training
configuration, metrics for all splits, calibration report, label threshold,
configuration hash, and artifact hash.

Activation requires:

- a frozen compatible same-channel/same-target dataset;
- current feature schema;
- label threshold;
- finite parameters with exact feature dimensions;
- complete train/validation/test metrics;
- matching artifact hash;
- explicit integrity, schema, isolation, quality, calibration-truth, and
  offline gates.

Rollback preserves the candidate and restores the prior qualified parent.

Calibration is truthful: fewer than 30 held-out pairs is insufficient, and even
when a held-out Brier audit can run, no independent calibrator means the model
remains labeled uncalibrated.

Production state:

- datasets: none;
- model versions: none;
- active caption/image/pairing model IDs: none;
- pairwise labels: 0.

No creator-trained quality or calibration claim is made.

## 9. Feedback normalization

Production began with 9 preserved legacy `caption_feedback` rows and zero
normalized signals.

Idempotent reconciliation produced:

- 9 caption signals;
- 9 image signals;
- 9 pairing signals;
- 27 total;
- 0 conflicts;
- 0 legacy rows missing normalized mappings.

A second reconciliation produced zero new records, and verification passed.

The caption context/ranking read path now queries channel-scoped normalized
signals. The legacy table remains a compatibility and audit record; existing
write paths preserve it and immediately derive canonical target signals.
Proposal/archive display may still read legacy rows to show the original
human-entered feedback record, but learning no longer depends on that legacy
query.

## 10. Annotation and representation backfills

Annotation coverage:

- eligible posts: 698;
- compatible current annotations: 698;
- missing: 0;
- reviewed annotations: 10;
- corrections: 7;
- current production version: historical annotation v2;
- planned v3 refresh runs: 0;
- Codex/model calls made for refresh: 0;
- cost: 0.

The implemented refresh lifecycle freezes exact source hashes and provider/
model/prompt/annotation identities, checkpoints attempts, preserves old raw
output, applies compatible corrections, reports incompatible corrections, and
validates coverage. Real model calls require `--allow-model-calls`.

Representation backfill:

- text v2: 698 complete, 0 stale/missing/failed;
- image v3: 716 complete, 0 stale/missing/failed;
- multimodal v2: 698 complete, 0 stale/missing/failed;
- total active planned records: 2,112;
- local deterministic provider calls only;
- remote calls/downloads/GPU/paid cost: 0.

The full v3 annotation refresh is deferred because it would consume Codex
allowance and was not authorized for unattended execution.

## 11. Provider research and adapters

The provider matrix records official source, license, approximate size,
resource profile, Runway relevance, and activation decision for:

- Qwen3 Embedding 0.6B;
- BGE-M3;
- SigLIP 2 Base;
- DINOv3 candidates;
- ColPali/ColQwen candidates;
- BGE and Jina rerankers;
- SEARLE/iSEARLE composed retrieval;
- FLUX.1 Kontext;
- an explicit future remote generation tier.

Implemented adapters:

- `sentence-transformers` text, explicit trusted local path;
- `siglip2` text/image/multimodal, explicit trusted local path.

Both are lazy, optional, local-files-only, `trust_remote_code=False`, CPU by
default, and diagnosable without loading weights. No automatic download,
implicit activation, remote provider, or fallback exists.

Production diagnostics:

- active default: deterministic `runway-local`;
- optional dependencies ready: false;
- local model paths configured: false;
- trained providers evaluated: 0;
- trained providers activated: 0.

The adapters remain inactive because this laptop has no provisioned weights or
validated resource budget, and no frozen Qlob or blind creator evaluation has
earned activation.

## 12. Retrieval

Canonical hybrid retrieval now:

- resolves exact active text/image/multimodal sets;
- persists set IDs, provider/model/version/configuration, and plan hashes;
- persists every considered, excluded, fused, selected, and negative-evidence
  record;
- supports multiple references, optional modification text, structured
  constraints, source/rights policy, excluded entities, and MMR diversity;
- preserves lexical, entity, topic, action, emotion, scene, composition,
  caption-structure, recent, visual, semantic, and multimodal channels;
- records exact, active, miss, recomputation, stale, and fail-closed counters.

The composed baseline is explicitly deterministic; it is not presented as a
learned composed-image retrieval model.

Production repeated-run evidence:

- run 3 resolved active sets 4/2/5 and had 1,952 active hits, 1 exact hit,
  3 first-use query misses/recomputations, 0 stale misses, latency 9,870.07 ms;
- run 4 used the same input and sets and had 1,952 active hits, 4 exact hits,
  0 misses, 0 recomputations, 0 stale misses, latency 9,307.13 ms;
- run 4 selected 18 evidence records;
- paid provider calls: 0.

The repeated pass therefore observed a 100% cache-hit path for addressable
representations. The two latencies are a smoke measurement, not a statistically
supported performance improvement.

Recall@k, nDCG, and creator relevance did not receive new genuine labels in this
milestone, so no retrieval-quality superiority is claimed.

## 13. Image generation

The provider harness supports explicit capability, instruction, seed, reference
eligibility, source/rights/safety result, output content hash, generation run,
agent run, and parent/child asset lineage.

The deterministic mock provider:

- works offline;
- records complete lineage;
- links its typed agent run;
- produces a candidate image;
- re-enters ordinary validation, duplicate, rights/safety, and human-review
  safeguards;
- never publishes.

No real provider is registered or enabled. FLUX-like local models are unsuitable
for the current hardware/license envelope; remote providers would be explicit,
paid, networked, and require separate authorization and evaluation.

## 14. Blind study and active learning

Blind-study tooling:

- preregisters target, baseline/challenger identity, seed, primary/secondary
  outcomes, case hashes, group/split, and duplicate-cluster limit;
- requires unique channel-owned media;
- deterministically randomizes display order and side;
- exports at least 50 cases by default;
- strips hidden origin/provider/model/rank/score/configuration fields;
- imports only explicit genuine reviewer responses;
- rejects duplicate labels;
- links non-tie human preferences to exact study responses and immutable
  features;
- reports descriptive wins/losses/ties/edits without fabricated significance.

Production state:

- planned studies: 0;
- exported study cases: 0;
- imported human responses: 0;
- synthetic creator labels: 0;
- pending: creator completion of a genuine 50-case review.

Active learning persists deterministic target-specific queues using uncertainty,
component disagreement, expected information, rarity, and duplicate-cluster
diversity. It preserves group/split boundaries and excludes final holdout.
Production batches remain 0; fixture selection is tested.

## 15. Intelligence database doctor

The doctor has human and JSON output and a nonzero exit for critical findings.
It checks:

- SQLite integrity, foreign keys, migration, schema contract, WAL, and
  publishing state;
- channel isolation across every intelligence family;
- representation active-set uniqueness, coverage, identity, source hashes,
  media paths, vector contracts, and invalid/stale records;
- annotation coverage and correction compatibility;
- retrieval run status, evidence, active-set provenance, and cache diagnostics;
- caption slate/candidate/exposure/proposal consistency;
- duplicate derivations, feature snapshots, split leakage, target separation,
  and label provenance;
- preference dataset/model artifact integrity, feature compatibility, metrics,
  calibration truth, activation, and files;
- feedback reconciliation and idempotency;
- agent terminal state, step uniqueness, budgets, linked runs, fallback, and
  publishing capability exclusion;
- generation eligibility, rights/safety lineage, channel, and review re-entry;
- experiment artifacts, metrics, activation references, and holdout use.

The first production run found three critical zero-norm vectors in active text
v1. Deterministic text/multimodal v2 fixed the source issue, full replacement
sets were backfilled and validated, and activation superseded the old sets.

Final production status:

- critical: 0;
- warning: 1;
- information: 2;
- warning: 3 invalid vectors remain only in preserved inactive set 1;
- information: SQLite integrity `ok`;
- information: 5 valid read-through query records exist outside immutable
  historical sets;
- overall: passed.

## 16. Testing

All production commands in this section inherited:

```powershell
$env:RUNWAY_DATA_DIR = "data/qlob-production"
$env:RUNWAY_PUBLISHING_ENABLED = "false"
$env:RUNWAY_AGENT_RUNTIME = "mock"
```

### Focused development checks

| Exact command | Scope and relevance | Result |
| --- | --- | --- |
| `.\.venv\Scripts\python.exe -m pytest tests/unit/test_intelligence_foundations.py -q --tb=short` | Deterministic provider/tokenless fallback and shared foundations | 6 passed |
| `.\.venv\Scripts\python.exe -m pytest tests/unit/test_neural_providers.py -q --tb=short` | Optional dependency, local-path, no-fallback, and no-download behavior | 4 passed |
| `.\.venv\Scripts\python.exe -m pytest tests/integration/test_representation_lifecycle.py -q --tb=short` | Plan/resume/validate/activate/rollback/cache/invalid vector/isolation | 4 passed |
| `.\.venv\Scripts\python.exe -m pytest tests/integration/test_intelligence_doctor.py -q --tb=short` | Active conflicts, stale vectors, duplicate labels, leakage, missing artifacts | 3 passed |
| `.\.venv\Scripts\python.exe -m pytest tests/integration/test_intelligence_evidence.py -q --tb=short` | Decision/edit/source/idempotency evidence | 3 passed |
| `.\.venv\Scripts\python.exe -m pytest tests/integration/test_caption_learning.py -q` | Canonical normalized feedback and immediate learning context | 1 passed after reconciliation fixes |

An intermediate broad suite reported 120 passed and one failure because an
existing assertion still expected migration `0007`. The assertion was updated
to the canonical head and its focused tests passed. This was a stale test, not a
production migration failure.

### Migration and production checks

| Exact command | Scope and relevance | Result |
| --- | --- | --- |
| `.\.venv\Scripts\python.exe -m runway.cli.main init` | Exact backup rehearsal, then production Alembic upgrade | Both reached `0008`; production only after rehearsal matched |
| `.\.venv\Scripts\python.exe -m runway.cli.main database schema-verify` | Full live structural comparison | `matches: true`; expected and observed fingerprints equal |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence feedback reconcile` (twice) | Legacy-to-canonical idempotency | First produced canonical mappings; second created 0 |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence feedback verify` | Missing/conflicting/duplicate/wrong-channel mappings | Passed, no findings |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations plan --modality text --provider runway-local` | Immutable text-v2 production plan | Set 4, 698 expected |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations plan --modality multimodal --provider runway-local` | Immutable multimodal-v2 production plan | Set 5, 698 expected |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations backfill --set-id 4 --batch-size 100 --until-complete` | Bounded/resumable text-v2 backfill | 698 complete, 0 failed/stale/pending |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations backfill --set-id 5 --batch-size 100 --until-complete` | Bounded/resumable multimodal-v2 backfill | 698 complete, 0 failed/stale/pending |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations verify --set-id 4` | Exact identity/hash/dimension/norm/contract validation | Valid |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations verify --set-id 5` | Exact identity/hash/dimension/norm/contract validation | Valid |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations activate --set-id 4 --reason "Replace text v1 with validated tokenless-caption fallback v2; focused regression suite passed" --gate-results data\qlob-production\reports\intelligence-v2-zero-vector-activation-gates.json --yes` | Transactional text activation/supersession | Set 4 active; set 1 superseded |
| `.\.venv\Scripts\python.exe -m runway.cli.main intelligence representations activate --set-id 5 --reason "Replace multimodal v1 to bind validated text fallback v2; focused regression suite passed" --gate-results data\qlob-production\reports\intelligence-v2-zero-vector-activation-gates.json --yes` | Transactional multimodal activation/supersession | Set 5 active; set 3 superseded |
| `.\.venv\Scripts\python.exe -m runway.cli.main embeddings backfill --limit 2` (twice) | Same-candidate retrieval/cache smoke | Runs 3/4; second had 0 misses/recomputations |
| `.\.venv\Scripts\python.exe -m runway.cli.main retrieval inspect --run-id 4` | Active identity/evidence/cache audit | Sets 4/2/5; 1,952 active hits, 4 exact hits |
| `.\.venv\Scripts\python.exe -m runway.cli.main database intelligence-doctor --json` | Full production health with media hashes | Passed: 0 critical, 1 warning, 2 information |
| Python standard-library SQLite `PRAGMA integrity_check` / `PRAGMA foreign_key_check` and row-count audit | Final database/data preservation | `ok`, 0 FK violations, all source counts preserved |
| `Get-FileHash -Algorithm SHA256` on production and backup databases | Backup/provenance verification | Production `8d41c65c...15ff8`; backup `9cafe2da...91d15` |

### Required final validation

| Exact command | Scope and relevance | Result |
| --- | --- | --- |
| `$env:RUNWAY_PUBLISHING_ENABLED='false'; $env:RUNWAY_AGENT_RUNTIME='mock'; .\.venv\Scripts\python.exe -m pytest -q` | Complete backend regression suite because shared models/protocols changed | 123 passed in 443.66s; one third-party Starlette deprecation warning |
| `.\.venv\Scripts\python.exe -m pytest tests/integration/test_canonical_migration.py tests/integration/test_schema_contract.py -q --tb=short` | Independent empty/current migration and structural contract proof | 6 passed |
| `.\.venv\Scripts\ruff.exe check src tests alembic` | All backend, test, and migration lint | All checks passed |
| `.\.venv\Scripts\python.exe -m mypy --strict --no-incremental src\runway` | Full strict type check after shared protocol changes | Success, 92 source files |
| `git diff --check` | Whitespace and conflict-marker safety | Passed |
| `rg` tracked-worktree credential pattern scan excluding `.env`, data, virtualenv, node modules, and databases | Secret safety before commit | No candidate secrets |
| `git status --short` and ignored-data inspection | Scope and production-artifact isolation | Only intended source/docs/tests; production data ignored |
| `git check-ignore -v .idea` | Requested IDE metadata exclusion | `.idea/` ignored by `.gitignore:26` |

The direct `mypy.exe --strict` parallel invocation once exited 1 with no output.
It was rerun through the project Python with `--no-incremental`; that diagnostic
run is the recorded authoritative result and passed.

Major checks intentionally not run:

- frontend `npm` checks/builds: no frontend code or consumed API response
  contract changed;
- frontend browser/E2E tests: no UI behavior changed;
- npm audit: unrelated to the Python/database milestone;
- live browser, YouTube publisher, scheduling, or production mutation tests:
  explicitly forbidden and publisher code was not changed;
- real Codex annotation refresh: consumes user allowance and requires explicit
  authorization;
- optional neural-provider inference: dependencies/weights are absent and tests
  must not download models;
- GPU tests: no GPU is required or authorized;
- real image generation: no provider is enabled or authorized;
- completed human blind study: genuine creator responses are pending and cannot
  be fabricated.

## 17. Measured improvements

Evidence-supported improvements:

- schema reproducibility moved from implicit ORM-era assumptions to a
  structurally compared 48-table contract with identical clean/upgrade
  fingerprint;
- historical retrieval now resolves complete explicit active sets and a repeated
  production pass recomputed zero representations;
- active deterministic representation coverage is complete across all three
  historical scopes;
- vector validation now catches nonfinite, count, norm, and cross-record
  contract errors before activation;
- the three tokenless-caption zero vectors were removed from active service by
  a versioned, deterministic, rollback-preserving replacement;
- nine legacy records now produce 27 correctly separated canonical targets;
- replay and selected-then-accepted derivation no longer duplicate equivalent
  learning rows;
- human edits now carry independent verification, immutable features, and an
  exact representation;
- scoring no longer trains in the request path;
- training/dataset/artifact hashes are reproducible in fixtures;
- provider absence produces a clear failure rather than fallback;
- doctor coverage expanded from general dependency checks to canonical
  intelligence integrity with enforced nonzero critical exit.

Not measured or not supported:

- no creator preference improvement;
- no learned semantic retrieval improvement;
- no calibrated model performance;
- no statistically reliable latency improvement;
- no audience engagement lift;
- no real image-generation quality;
- no neural-provider superiority.

What is now exceptional is evidence discipline: exact source identity,
append-only derivation, immutable decision-time features, complete activation
lineage, production rehearsal, fail-closed diagnostics, and honest separation
of implemented capability from measured quality.

## 18. Known limitations

- Production still uses deterministic, hand-engineered representations.
- The text fallback now handles tokenless captions correctly but is not a
  learned semantic encoder.
- The multimodal baseline concatenates deterministic image/text information; it
  is not a learned aligned composed-retrieval model.
- Production has zero pairwise labels and no active preference model.
- The nine existing decisions provide normalized feedback but do not meet model
  training or calibration thresholds.
- The v3 annotation refresh is implemented but not run.
- Optional neural adapters cannot be evaluated on this laptop without trusted
  weights/dependencies and a practical resource plan.
- Blind study and active-learning production artifacts await creator input.
- Retrieval smoke latency is roughly 9.3 seconds for the exercised full local
  candidate context and needs broader profiling.
- The doctor intentionally reports preserved invalid inactive v1 rows as a
  warning; removing them would weaken rollback/audit evidence.
- SQLite is appropriate for the local single-user deployment but still requires
  writer isolation and correct backup handling during migration.
- Real generation and platform publishing remain separate, disabled concerns.

## 19. Risks

- Label scarcity may tempt premature training or overinterpretation of a small
  study; activation thresholds and calibration labels must remain fail-closed.
- Creator behavior may drift, requiring time-aware evaluation without deleting
  historical evidence.
- Optional provider licenses, revisions, dependencies, and resource costs can
  change; every exact artifact requires renewed review.
- A stronger public model may still regress Qlob grounding, duplicate safety,
  rights policy, latency, or creator preference.
- SQLite schema migrations that recreate tables require stopped writers and a
  verified online backup.
- Browser-based discovery and publishing remain structurally fragile to
  external UI changes, though they are outside the intelligence harness.
- Representation storage will grow as versions are preserved; retention must
  never delete rollback evidence without an explicit archival policy.
- Doctor warnings must be reviewed, not normalized into ignored noise.

## 20. Recommended next milestone

The next milestone should be **Creator Evidence and First Qualified Preference
Model**, not another architecture expansion.

Recommended sequence:

1. Continue ordinary Runway review to collect genuine, target-separated
   accept/edit/reject/replace decisions with no additional training form.
2. Use active learning to present diverse, uncertain development/tuning cases
   while preserving untouched holdout groups.
3. Complete a preregistered creator-blind study of at least 50 untouched
   representative caption cases.
4. Inspect failure clusters and update only evidence-supported features or
   policies.
5. Freeze the first sufficiently sized caption dataset, train the persisted
   pairwise model, report held-out accuracy/edit/acceptance and calibration
   truth, and activate only if all hard gates pass.
6. Evaluate one optional trained text representation on separate suitable
   hardware or an explicitly authorized environment, using the same frozen case
   IDs, latency/resource accounting, and blind creator procedure. Keep it
   inactive unless it provides relevant measured value.
7. Defer image and pairing models until each target has enough genuine labels.

Runway now has an unusually strong architecture and evidence discipline for a
local creator-specific system. It does not yet have evidence that its
deterministic representations or generated captions are world-best. The next
real gain must come from genuine creator labels and frozen comparative
evaluation, not more unvalidated components.
