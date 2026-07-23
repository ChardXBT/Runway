# RunWay neural intelligence and multimodal quality upgrade

Status date: 2026-07-22

This report records the implementation and production-data rehearsal for the
canonical RunWay intelligence engine. It deliberately distinguishes
architecture from evidence. A component is not described as neural, evaluated,
or production-active unless stored evidence supports that statement.

## Executive result

RunWay now has one auditable path for active-representation resolution,
three-arm blind evaluation, neural challenger evaluation, joint reranking,
claim-level grounding, multi-provider discovery, adaptive parent runs, learned
channel modes, slate optimization, hard-negative mining, composed-retrieval
datasets, and candidate-exposure metadata.

The deterministic representation sets remain active. No trained model was
activated. No model weights were present, downloaded, or committed. No paid API,
browser, image-generation provider, or YouTube publishing action was invoked.

The evidence does **not** currently support either claim:

```text
RunWay + frontier > raw frontier
RunWay + weaker model > raw frontier
```

Both claims remain blocked on genuine creator review in the preregistered arena.

| Area | State | Evidence |
| --- | --- | --- |
| Canonical semantic resolver | implemented and tested | Active-set identity is attached to every score; provider mismatch fails closed. |
| Evidence provenance split | implemented and tested | Retrieval-selected, model-supplied, model-cited, and ranker-used IDs are independent. |
| Semantic topic eligibility | implemented and tested | Five classes; only explicit blocks and confident off-topic results hard reject. |
| Three-arm frontier arena | implemented, inactive | Blinded export/import works; production has zero cases and creator responses. |
| Trained text challengers | evaluation-ready, blocked | Qwen3 Embedding 0.6B, BGE-M3, and MiniLM require pinned local weights and labels. |
| Trained image-text challenger | evaluation-ready, blocked | SigLIP 2 requires pinned local weights and labels. |
| Unified multimodal challengers | experimental boundary | GME and VLM2Vec-V2 have no adapter or activation path. |
| Joint multimodal reranker | implemented, shadow-only | Interpretable dimensions persist; zero production runs exist yet. |
| Claim-level grounding | implemented, partial activation | Deterministic layer works; independent VLM verification lacks an evaluated local model. |
| Discovery ensemble | implemented and tested | Providers merge with URL deduplication and contribution provenance. |
| Adaptive parent pipeline | implemented and tested | One parent run links retrieval through slate selection; it cannot publish. |
| Learned content modes | active on deterministic space | Qlob profile v5 has seven modes; `neural=false` is recorded. |
| Neural/session diversity | implemented; neural activation blocked | It resolves the active semantic space, which remains deterministic. |
| Hard negatives | implemented and production-rehearsed | 329 policy-labelled pairs; replay created zero; human labels created: zero. |
| Learned composed retrieval | evaluation-ready boundary, blocked | Production has zero reviewed creator examples and no learned provider. |
| Exposure-bias metadata | implemented, prospective | Production has zero post-upgrade candidate exposures. |

## 1. Repository and audit baseline

The requested reference commit was `309af5c65a6491b8fe5dfdd93357194cafa9504f`.
The actual `origin/main` baseline had advanced to
`e0d798881b9ff114d100728c18fd7c2c7b80a1be`, so the newer work was inspected and
preserved.

The production audit confirmed:

- 771 captured Qlob posts;
- 698 training-eligible image-caption posts;
- 716 historical media assets, plus candidate/preview/approved working assets;
- active text set 4: 698/698, `runway-local/signed-subword-concepts`;
- active image set 2: 716/716, `runway-local/image-descriptor`;
- active multimodal set 5: 698/698,
  `runway-local/image-text-concatenation`;
- 25 genuine human pairwise labels, all for the caption target;
- zero human image labels, pairing labels, or frontier-arena responses;
- zero active preference models.

The catalogue counts in the upgrade request were current. The description of
trained neural intelligence was aspirational: all active representation sets
were and remain deterministic. Existing neural adapters were inactive and no
local weights were configured.

## 2. Correctness changes

### Active representation resolution

`ActiveRepresentationResolver` is now the canonical entry point for semantic
vectors and similarities. It is used by retrieval, candidate ranking, creator
feedback similarity, caption duplication, positive/negative feedback matching,
reference retrieval, diversity clustering, image-caption compatibility, and
slate optimization.

When an active set exists, a requested provider/model/configuration mismatch is
an error rather than a silent deterministic fallback. If no lifecycle set exists,
the resolver may use the configured deterministic rollback provider and records
that resolution. Legacy `MediaAsset.embedding_vector` values no longer control
primary semantic ranking; they remain for historical diagnostics and
non-semantic duplicate safeguards.

Every persisted semantic result records representation-set ID, provider, model,
model version, configuration hash, and resolution mode.

### Evidence provenance

Caption slates now keep four independent collections:

1. evidence selected by retrieval;
2. evidence supplied to the model;
3. evidence explicitly cited by the model;
4. evidence used by the final ranker.

An empty citation list is valid. Retrieved IDs are never copied into the cited
field merely because they were available in the prompt.

### Topic eligibility

Exact topic intersection is no longer the general rejection rule. Eligibility
uses active text semantics, aliases, annotation confidence, related-topic
distance, and explicit exploration policy. Results are `supported`, `adjacent`,
`exploratory`, `off-topic`, or `blocked`. Explicit creator blocks remain hard
constraints; adjacent and exploratory content remain eligible with a penalty.

### Read-only schema inspection

`database schema-status` and `database schema-verify` previously invoked the
auto-migrating initializer despite being documented as inspection commands.
They now open the existing SQLite database with `mode=ro`, fail if it does not
exist, and never invoke Alembic. A regression test changes a temporary revision
marker and proves `schema-status` reports rather than upgrades it.

## 3. Current evaluation reporting

Profile evaluation now separates current-pipeline observations from historical
diagnostics. The Qlob profile-v5 production report recorded:

| Metric | Value | Interpretation |
| --- | ---: | --- |
| Active representation coverage | 100% for all three sets | Coverage only; not neural-quality evidence. |
| Retrieval evidence selected | 870 records across 49 runs | Current canonical retrieval activity. |
| Displayed caption candidates | 69 | Small observational sample. |
| Deterministic grounding pass rate | 100% | Not a human claim-level audit. |
| Caption exposures | 16 | Small observational sample. |
| Observed no-edit acceptance | 56.25% | Descriptive, not randomized or arena-controlled. |
| Human caption preference labels | 25 | Below the 100-label product challenger minimum. |
| Human-labelled retrieval nDCG / Recall@k | unavailable | No suitable labelled retrieval set exists. |
| Frontier arena cases/responses | 0 / 0 | No superiority inference is permitted. |

These remain historical diagnostics, not the definitive score of the current
engine:

- image-caption projection accuracy: 2.1429% on 140 holdout examples;
- generic-versus-channel caption ranking: 25%;
- legacy visual top-three topic relevance: 95.7143%;
- reviewed annotation-field accuracy: 20% over 20 fields from a deliberately
  non-random uncertainty/outlier sample.

The low projection, caption-ranking, and annotation-review numbers are material
quality warnings. They strengthen the case for labelled neural evaluation; they
must not be hidden behind an architecture upgrade.

## 4. Three-arm raw-frontier arena

The arena persists fixed model identities and the same candidate image across:

- Arm A: raw frontier with a fair prompt, compact history, and examples;
- Arm B: the same frontier model through RunWay intelligence;
- Arm C: a cheaper/weaker model through the same RunWay intelligence.

Exports randomize caption positions and omit source identities. Imports capture
preferred and acceptable captions, edits, image/caption/pairing verdicts, reason
codes, grounding issues, genericness, repetition, decision time, model cost, and
latency. Engineering fixtures use `reviewer_kind=engineering_fixture`; they do
not increment human response counts or creator-truth labels.

Directional reporting requires 50 genuine creator cases. The format supports at
least 150 mature paired cases and a preregistered 60% preference target with
confidence analysis. Production has no arena study, so no arm win rate exists.

## 5. Neural representation challengers

The offline evaluator catalogues:

- `Qwen/Qwen3-Embedding-0.6B` with instruction-aware query formatting;
- `BAAI/bge-m3` with instruction-aware query formatting;
- `sentence-transformers/all-MiniLM-L6-v2` as the smaller text challenger;
- `google/siglip2-base-patch16-224` for aligned image-text retrieval;
- GME and VLM2Vec-V2 as unimplemented research boundaries.

The evaluator accepts only a caller-supplied local model directory and pinned
revision. It never downloads weights or activates a representation set. It
reports MRR, Recall@1/3 where applicable, latency, hardware, local artifact size,
human-label count, licence state, and all remaining activation gates.

Production readiness result:

- Qwen3, BGE-M3, MiniLM, and SigLIP 2: `blocked_missing_local_weights`;
- GME and VLM2Vec-V2: `adapter_unavailable`;
- trained-model quality evaluations completed: zero;
- neural representation sets activated: zero.

This is a blocked evaluation, not a failed challenger and not evidence that the
deterministic model is better.

## 6. Joint reranking and grounding

The shadow reranker evaluates plausible combinations and persists factual
grounding, caption quality, image quality, image-caption compatibility, creator
preference, style fit, novelty, rotation fit, session fit, uncertainty, and final
score. It stores input, provider/model/prompt, configuration, representations,
baseline and final orders, latency, and whether ordering changed.

The current implementation is an interpretable policy baseline in shadow mode,
not a trained neural reranker. Teacher labels remain separate from human labels.
Production reranker runs are zero because no post-upgrade caption generation was
performed during this milestone.

Grounding now extracts atomic claims and stores classification, confidence,
evidence, provider, model, and prompt version. Deterministic verification remains
the fast first layer. An independent semantic verifier interface exists, but is
inactive until an evaluated local VLM is configured. Critical unsupported
identity, event, quote, or relationship claims fail the deterministic gate; the
requested sub-1% general unsupported-claim target is unmeasured.

## 7. Discovery and adaptive agent pipeline

The discovery orchestrator can merge creator-owned, historical, approved-library,
Frinkiac, browser search, permitted API, licensed, and explicitly enabled
generated providers. Query families cover entity, scene, action, emotion,
composition, visual neighbour, reference image, and reference-plus-modification
intents. The merged pool records provider contribution, deduplicates canonical
URLs, and carries provider metadata into candidate and search-run provenance.

One persisted parent `editorial_pipeline` run now links actual artifacts for:

```text
retrieval -> coverage -> content mode -> angle planning -> generation lanes
-> verification -> optional claim grounding -> joint reranking -> slate optimization
```

Routing classifies easy, uncertain, and high-risk cases. Caption generation uses
versioned angle lanes for curiosity, reaction, observation, comparison,
prediction, explanation, and channel-specific modes. Selective retries and typed
abstention remain bounded. The capability registry rejects publishing actions.

## 8. Channel modes and diversity

Content modes are learned per channel with k-medoids over the active multimodal
representation set. Each stores medoid/representative posts, caption and visual
prototypes, entities, scenes, structures, editorial angles, feedback placeholders,
recent exposure, and fatigue.

Qlob's pre-upgrade active profile had no modes. The idempotent, model-free
`profile content-modes-backfill` command created immutable profile v5 from v4;
replay returned `unchanged`. It learned seven modes over 558 training examples
using active set 5. Because set 5 is deterministic, the profile records
`neural=false`. Several labels collapse to similar “observation / Homer” modes;
that is a visible limitation of the current semantic space, not a result to hide
with hardcoded names.

Slate optimization evaluates the complete slate rather than independent top
items. It combines relevance, grounding, creator preference, mode coverage,
novelty, pairwise similarity, source repetition, and session fatigue. Rolling
budgets cover topic, entity, scene, setting, emotion, composition, source,
visual cluster, caption structure, first word, angle, and content mode.
Deterministic diversity families remain guardrails.

## 9. Preference learning, hard negatives, and exposure bias

Caption, image, and pairing targets remain isolated. Label sources distinguish
human, teacher, synthetic, policy, and audience evidence. Product activation now
requires at least 100 genuine human labels for the target; the eight-label test
floor is explicitly an engineering-fixture threshold.

Production hard-negative mining created 329 caption comparisons:

| Category | Count |
| --- | ---: |
| grounded versus unsupported | 125 |
| novel versus near duplicate | 73 |
| same scene, different angle | 58 |
| specific versus generic | 55 |
| fresh versus recent overuse | 18 |

Every row is `label_source=policy`, is not product-training eligible, and created
no human label. Replaying the same mine planned 329 and created zero.

Candidate exposures can record shown, skipped, replaced, rejected, accepted, and
`Fewer like this`, including pre-display score, display probability, position,
policy, randomization flag, eligible-pool size, selection/withholding reason, and
active representation snapshot. Safe randomization is not enabled automatically.
Production has zero prospective candidate-exposure rows, so propensity-based
evaluation is not yet possible.

## 10. Composed retrieval

`composed_retrieval_examples` stores reference image, modification instruction,
target image, source, review state, split, representations, and evaluation.
Weighted vector fusion remains explicitly a deterministic comparison baseline,
never learned retrieval. Production is blocked by zero reviewed human examples
(minimum 50) and no configured evaluated learned provider.

## 11. Migration, production rehearsal, and rollback

Alembic revision `0010_neural_intelligence` is additive. It adds split evidence
provenance, topic/exposure metadata, three-arm study fields, claim/reranker
metadata, and three tables:

- `candidate_exposures`;
- `multimodal_rerank_runs`;
- `composed_retrieval_examples`.

Clean-install and upgrade schemas converge on:

```text
migration:   0010_neural_intelligence
tables:      52
fingerprint: cd024bcb9cd41464a3319145577c3dbdf51cf2d2229943237d8255abf0f445d2
```

Verified local recovery points (ignored by Git) are:

```text
data/qlob-production/backups/runway-pre-0010-20260722T150934.db
data/qlob-production/backups/runway-post-0010-20260722T150934.db
data/qlob-production/backups/runway-final-neural-20260722T153626.db
```

The pre-upgrade copy is revision `0009_editorial_diversity`; the post-upgrade
copy is `0010_neural_intelligence`. The final snapshot includes the profile-mode
and policy-hard-negative backfills and has SHA-256
`0B5C444FD7A283790CE98E6D9B152B9D8E5789C4750B55813631C31532D45EE4`.
All three returned `integrity_check=ok` and zero foreign-key violations.
Migration downgrade and deterministic-set rollback are covered by tests. No
recovery point was overwritten.

Final production verification reported:

- schema matches the committed contract;
- `integrity_check=ok` and zero foreign-key violations;
- intelligence doctor: zero critical, two warnings, two information findings;
- warning: three invalid vectors exist only in preserved inactive sets;
- warning: 21 legacy displayed captions predate prospective exposure capture.

Those warnings are retained because rewriting historical vectors or inventing
past display propensities would create false evidence.

## 12. Cost and latency

This milestone made no paid inference calls, so measured provider cost is $0.
No neural inference latency is reported because no local weights were available.
Production profile evaluation completed locally in approximately nine seconds;
hard-negative mining completed in approximately eight seconds. These are command
timings, not model benchmarks.

## 13. Operations

Use the explicit maintenance envelope:

```powershell
$env:RUNWAY_DATA_DIR = "data/qlob-production"
$env:RUNWAY_PUBLISHING_ENABLED = "false"
$env:RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED = "false"
$env:RUNWAY_AGENT_RUNTIME = "mock"
```

Read-only schema status:

```powershell
.\.venv\Scripts\python.exe -m runway.cli.main database schema-status
.\.venv\Scripts\python.exe -m runway.cli.main database schema-verify
```

No-download challenger readiness:

```powershell
.\.venv\Scripts\python.exe -m runway.cli.main intelligence neural readiness `
  --output data\qlob-production\reports\neural-challenger-readiness.json
```

Pinned local evaluations:

```powershell
.\.venv\Scripts\python.exe -m runway.cli.main intelligence neural evaluate-text `
  --challenger qwen3-embedding-0.6b `
  --model-path C:\models\qwen3-embedding-0.6b `
  --revision <verified-revision> `
  --dataset data\evaluation\text-retrieval.json `
  --license-verified

.\.venv\Scripts\python.exe -m runway.cli.main intelligence neural evaluate-image-text `
  --challenger siglip2-base-patch16-224 `
  --model-path C:\models\siglip2-base-patch16-224 `
  --revision <verified-revision> `
  --dataset data\evaluation\image-text-retrieval.json `
  --license-verified
```

Content modes, hard negatives, and evidence reports:

```powershell
.\.venv\Scripts\python.exe -m runway.cli.main profile content-modes-backfill
.\.venv\Scripts\python.exe -m runway.cli.main profile evaluate
.\.venv\Scripts\python.exe -m runway.cli.main intelligence hard-negatives-mine
.\.venv\Scripts\python.exe -m runway.cli.main intelligence exposure-report
.\.venv\Scripts\python.exe -m runway.cli.main intelligence composed-retrieval readiness
```

Three-arm arena lifecycle:

```powershell
.\.venv\Scripts\python.exe -m runway.cli.main intelligence arena plan `
  --cases <fixed-arm-cases.json> --study-key <key> --target-cases 50
.\.venv\Scripts\python.exe -m runway.cli.main intelligence arena export `
  --study-id <id> --output <blind-review.json>
.\.venv\Scripts\python.exe -m runway.cli.main intelligence arena import `
  --study-id <id> --review <creator-review.json> --review-session <session>
.\.venv\Scripts\python.exe -m runway.cli.main intelligence arena report --study-id <id>
```

## 14. Changed implementation surface

- Schema/lifecycle: `alembic/versions/0010_neural_intelligence.py`,
  `src/runway/db/models.py`, `src/runway/intelligence/doctor.py`, and the schema
  contract snapshot.
- Semantic resolution/retrieval: `embeddings.py`, `representation_sets.py`,
  `retrieval.py`, `topic_eligibility.py`, and `ranking/service.py`.
- Captions/provenance: `captions/service.py`, `planning.py`, `feedback.py`,
  `exposures.py`, `preference_models.py`, `claim_grounding.py`, and
  `analysis/prompts/captions-v6.txt`.
- Evaluation: `evaluation/neural_challengers.py`, `frontier_arena.py`,
  `reranking.py`, `hard_negatives.py`, `composed_retrieval.py`, and
  `evaluation/experiments.py`.
- Discovery/modes/diversity: `discovery/providers.py`, `discovery/service.py`,
  `content_modes.py`, `slate_optimization.py`, `candidate_diversity.py`, and
  `exposure_bias.py`.
- Orchestration: `agent_harness.py`, `generation/service.py`,
  `proposals/service.py`, `analysis/runtime.py`, `api/intelligence_routes.py`,
  and `cli/main.py`.
- Configuration: `.env.example`, `config/defaults.yaml`, and
  `config/settings.py`.
- Tests: focused migration, representation, retrieval, caption provenance,
  grounding, discovery, agent, diversity, preference, arena, doctor, and CLI
  regression suites under `tests/integration` and `tests/unit`.

## 15. Validation completed

All automated checks used temporary databases, mock runtimes, local fixture
images, and no-download provider boundaries. No test opened a live browser or
published to YouTube.

| Command / scope | Result |
| --- | --- |
| Focused neural-upgrade integration suite | 5 passed |
| Read-only schema CLI regression suite | 4 passed |
| Broad intelligence/backend matrix | 69 passed in 310.65s |
| `ruff check src/runway tests alembic` | passed |
| `mypy src/runway` strict mode | passed, 107 source files |
| Production `database schema-verify` | exact migration/fingerprint match |
| Production intelligence doctor | passed, 0 critical findings |
| Production SQLite integrity / foreign keys | `ok` / 0 violations |

The broad matrix covers migration parity and downgrade, active-set lifecycle and
rollback, no-download provider loading, retrieval, caption provenance, grounding,
discovery, adaptive runs, diversity, preference-target isolation, blind studies,
channel isolation, deterministic fallback, and doctor invariants.

## 16. Known limitations and highest-value next action

Known limitations are evidence gaps, not hidden fallbacks:

- no local trained text, image-text, or independent visual-verifier weights;
- no human-labelled retrieval benchmark;
- only 25 genuine caption preferences and none for image/pairing targets;
- no three-arm creator responses;
- no post-upgrade production reranker or candidate-exposure runs;
- no reviewed composed-retrieval examples;
- no cross-channel histories for a real generalization comparison;
- current deterministic content-mode labels are visibly coarse;
- 21 legacy displays cannot receive truthful historical propensities.

The highest-value next action is to create the first 50 fixed-image arena cases,
have the creator review them blind, and record edits/reason codes. In parallel,
provide one pinned, licence-reviewed local text model and SigLIP 2 directory so
the exact same labelled cases can evaluate trained representations. That begins
proving the startup thesis instead of adding more unvalidated architecture.
