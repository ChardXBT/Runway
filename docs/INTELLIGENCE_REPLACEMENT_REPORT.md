# RunWay canonical intelligence replacement report

This report is the implementation record for the canonical intelligence-engine upgrade. It
distinguishes measured behavior from architecture, proxies from human labels, and completed work
from future experiments.

## 1. Architectural summary

### Original architecture

The frozen engine combined a Qlob-oriented style profile, hand-built image and text similarity,
bounded history retrieval, a fixed caption pool, deterministic ranking, and a proposal workflow.
It had strong local-first, immutable-source, duplicate, browser-publishing, and no-paid-fallback
boundaries. Its main intelligence weaknesses were global queries, first-image-only retrieval,
unpersisted retrieval/candidate decisions, universal question-first behavior, privileged
templates, generic filler instead of abstention, and feedback that did not cleanly separate the
image, caption, and pairing.

### Final canonical architecture

RunWay now has one channel-generic intelligence path:

```text
versioned channel history + explicit policy + recent mode + creator feedback
                                    |
                                    v
                         layered channel profile
                                    |
reference media + text instruction + constraints + source/rights policy
                                    |
                                    v
                  independent representation/retrieval pools
                                    |
                                    v
                       RRF fusion + MMR + role quotas
                                    |
                                    v
                     persisted bounded evidence package
                                    |
                                    v
               editorial brief -> candidate generation -> verifier
                                    |
                                    v
              preference/components ranking -> diverse eligible slate
                                    |
                         retry once or typed abstention
                                    |
                                    v
          exposure -> select/edit/accept/reject -> pairwise and target feedback
```

`RetrievalService`, `CaptionService`, `CandidateRanker`, and explicit provider registries are the
only production intelligence path. The old implementation survives only as immutable benchmark
artifacts and git history. There is no runtime engine-version selector or shadow engine.

Important refactors include channel-scoped queries, generic schemas/prompts, multi-image
representations, evidence persistence, explicit rights policy, layered profiles, composed
retrieval, grounded caption slates, preference/exposure records, separate feedback targets,
lineage-safe mock image generation, and deterministic evaluation/tuning tooling.

### Deviations and independently selected improvements

- Heavy semantic model downloads were deliberately not added. The default providers are
  deterministic local baselines behind versioned interfaces.
- Real image generation was deliberately not enabled. Only the offline mock provider can run.
- Deterministic policy labels were not mislabeled as human preference.
- Content-cluster split validation, typed abstention, field-level provenance, and pre-import test
  environment isolation were added because they were necessary for trustworthy evidence.
- No live YouTube mutation was used as intelligence validation.

## 2. Current-engine audit

### Strengths retained

- Immutable capture records and content-addressed media.
- Additive Alembic history and SQLite WAL.
- Typed model-output boundaries and raw model-run preservation.
- Exact and transformed duplicate safeguards.
- Explicit ChatGPT-authenticated Codex runtime with API-key stripping and no fallback.
- Guarded, serialized publishing outbox with conservative unverified handling.
- Deterministic fixtures and offline testability.

### Weaknesses corrected

- Global profile, retrieval, feedback, duplicate, ranking, and discovery reads could cross
  channels.
- Visual and text retrieval lacked a versioned provider/evidence contract.
- Historical multi-image posts were reduced to image zero in retrieval.
- Qlob language and question-first behavior appeared in reusable logic.
- Retrieval candidates, exclusions, scores, roles, and selections were not first-class records.
- Complete caption slates, displayed order, exposures, edits, and pairwise choices were not
  normalized evidence.
- One injected question template was privileged and generic fillers concealed failure.
- Rights uncertainty was often a score rather than a fail-closed policy decision.
- Image feedback did not materially influence discovery.
- Profile layers mixed explicit policy, long-term style, recent state, creator preference, and
  audience performance.
- One prompt identity claimed `captions-v3` while loading an older prompt.
- Similarity-edge rebuilding and several active-profile lookups were not channel-isolated.

### Incorrect assumptions found

- Complete candidate pools did exist in immutable `model_runs`; the missing layer was normalized
  per-candidate and per-exposure provenance.
- Multi-image annotation already existed; multi-image retrieval and similarity did not.
- The existing retrieval system was useful as an offline baseline, but handcrafted descriptors
  were not evidence of neural semantic retrieval.

### Hidden bottlenecks

- Sparse creator comparisons leave the preference model in deterministic fallback mode.
- Old production annotations remain on older schema versions until incrementally refreshed.
- The small labelled fixture sets cannot discriminate many ranking-weight changes.
- Browser and model runtime dominate real latency, while the current saved benchmark measures
  local deterministic overhead only.

### Qlob-specific assumptions found

`qlob_style_score`, universal open-question selection, franchise-first language, Qlob prompt
wording, and Qlob publisher/session strings were either made generic or moved to configured
channel data. Remaining Qlob references are configuration defaults, deployment documentation,
sanitized fixture identifiers, and the current capture fixture—not reusable intelligence logic.

## 3. Generalization work

- Analysis schemas use generic subjects, entities, actions, scenes, emotions, composition,
  visible text, uncertainty, language, and per-field confidence.
- Prompts no longer name Qlob or assume animation/franchise content.
- Channel name, handle, language, locale, policy, history, and profile version are explicit
  inputs.
- The Qlob open-question objective is an explicit channel policy and does not receive a universal
  rank bonus.
- Retrieval, feedback, profile, duplicate, discovery, proposal, dashboard, editorial, and
  internal-publishing queries are channel-scoped.
- Reference IDs supplied from another channel are rejected.
- Five synthetic histories—reaction, sports, science, gaming, and brand—were evaluated against
  one shared image.
- All five produced different captions, matched their configured structures, remained grounded,
  and passed policy isolation.

Evidence:

- `benchmarks/intelligence/experiments/generalization.json`
- `tests/integration/test_multichannel_generalization.py`
- `tests/integration/test_intelligence_evidence.py`
- `tests/unit/test_generic_annotation.py`

## 4. Exact files changed

### Frozen baseline

- `scripts/freeze_intelligence_baseline.py`
- `benchmarks/intelligence/baseline-876fe5f/README.md`
- `benchmarks/intelligence/baseline-876fe5f/checksums.sha256`
- `benchmarks/intelligence/baseline-876fe5f/dataset.json`
- `benchmarks/intelligence/baseline-876fe5f/manifest.json`
- `benchmarks/intelligence/baseline-876fe5f/metrics.json`
- `benchmarks/intelligence/baseline-876fe5f/outputs.json`

### Migration, models, and services

- `alembic/versions/0007_canonical_intelligence.py`
- `pyproject.toml`
- `src/runway/db/models.py`
- `src/runway/db/repositories.py`
- `src/runway/api/app.py`
- `src/runway/cli/main.py`
- `src/runway/catalog/service.py`
- `src/runway/discovery/service.py`
- `src/runway/editorial/service.py`
- `src/runway/proposals/service.py`
- `src/runway/publishing/internal.py`
- `src/runway/publishing/youtube.py`
- `src/runway/ranking/duplicates.py`
- `src/runway/ranking/service.py`
- `src/runway/services/dashboard.py`

### Analysis, intelligence, caption, and generation code

- `src/runway/analysis/features.py`
- `src/runway/analysis/runtime.py`
- `src/runway/analysis/schemas.py`
- `src/runway/analysis/service.py`
- `src/runway/analysis/prompts/annotate-history-batch-v3.txt`
- `src/runway/analysis/prompts/candidate-analysis-v2.txt`
- `src/runway/analysis/prompts/captions-v2.txt`
- `src/runway/analysis/prompts/captions-v3.txt`
- `src/runway/analysis/prompts/captions-v4.txt`
- `src/runway/intelligence/embeddings.py`
- `src/runway/intelligence/fusion.py`
- `src/runway/intelligence/policies.py`
- `src/runway/intelligence/profile.py`
- `src/runway/intelligence/retrieval.py`
- `src/runway/captions/exposures.py`
- `src/runway/captions/feedback.py`
- `src/runway/captions/planning.py`
- `src/runway/captions/preferences.py`
- `src/runway/captions/service.py`
- `src/runway/captions/taxonomy.py`
- `src/runway/captions/verification.py`
- `src/runway/generation/__init__.py`
- `src/runway/generation/providers.py`
- `src/runway/generation/schemas.py`
- `src/runway/generation/service.py`

### Evaluation code

- `src/runway/evaluation/__init__.py`
- `src/runway/evaluation/datasets.py`
- `src/runway/evaluation/experiments.py`
- `src/runway/evaluation/gates.py`
- `src/runway/evaluation/generalization.py`
- `src/runway/evaluation/reports.py`
- `src/runway/evaluation/runner.py`
- `src/runway/evaluation/scoring.py`
- `src/runway/evaluation/statistics.py`

### Tests

- `tests/conftest.py`
- `tests/integration/test_canonical_migration.py`
- `tests/integration/test_caption_learning.py`
- `tests/integration/test_caption_provenance.py`
- `tests/integration/test_discovery_captions.py`
- `tests/integration/test_foundation.py`
- `tests/integration/test_generation_lineage.py`
- `tests/integration/test_intelligence_evidence.py`
- `tests/integration/test_multichannel_generalization.py`
- `tests/unit/test_candidate_safeguards.py`
- `tests/unit/test_caption_identity.py`
- `tests/unit/test_caption_verification.py`
- `tests/unit/test_codex_runtime.py`
- `tests/unit/test_evaluation_harness.py`
- `tests/unit/test_generic_annotation.py`
- `tests/unit/test_intelligence_foundations.py`

### Datasets and evaluation outputs

- `benchmarks/intelligence/datasets/canonical-v1.json`
- `benchmarks/intelligence/datasets/generalization-v1.json`
- `benchmarks/intelligence/evaluations/canonical-development/candidate-results.json`
- `benchmarks/intelligence/evaluations/canonical-development/candidate-results.md`
- `benchmarks/intelligence/evaluations/canonical-tuning/candidate-results.json`
- `benchmarks/intelligence/evaluations/canonical-tuning/candidate-results.md`
- `benchmarks/intelligence/evaluations/canonical-locked-holdout/candidate-results.json`
- `benchmarks/intelligence/evaluations/canonical-locked-holdout/candidate-results.md`
- `benchmarks/intelligence/evaluations/canonical-locked-holdout/HOLDOUT_USE.json`
- `benchmarks/intelligence/experiments/ablation.json`
- `benchmarks/intelligence/experiments/ablation.md`
- `benchmarks/intelligence/experiments/critical-gates.xml`
- `benchmarks/intelligence/experiments/generalization.json`
- `benchmarks/intelligence/experiments/HOLDOUT_EVALUATED.json`
- `benchmarks/intelligence/experiments/holdout-comparison.json`
- `benchmarks/intelligence/experiments/holdout-comparison.md`
- `benchmarks/intelligence/experiments/holdout-result.md`
- `benchmarks/intelligence/experiments/paired-comparison.json`
- `benchmarks/intelligence/experiments/paired-comparison.md`
- `benchmarks/intelligence/experiments/production-migration-verification.json`
- `benchmarks/intelligence/experiments/replacement-gates.json`
- `benchmarks/intelligence/experiments/replacement-gates.md`
- `benchmarks/intelligence/experiments/tuning-summary.json`
- `benchmarks/intelligence/experiments/tuning-summary.md`

Each of the following preserved experiment directories contains exactly
`experiment.json`: `exp-27f1f2e06568`, `exp-37e7d7e744f0`, `exp-3ed430672233`,
`exp-603fa7b3c6d4`, `exp-69eba2385ca4`, `exp-73273c3d775f`,
`exp-780f79ba7e83`, `exp-8647a903a0e1`, `exp-935a6bf3fb5e`,
`exp-99c73932eeb7`, `exp-a8d271cf3bc0`, `exp-c751440b15da`,
`exp-cd3e195c0c66`, `exp-d28579b12fa6`, `exp-db58d3796da0`,
`exp-e0efbb6d6424`, `exp-e6701351ed6f`, and `exp-f006b75edb69`.

### Documentation

- `README.md`
- `docs/INTELLIGENCE_VNEXT.md`
- `docs/INTELLIGENCE_REPLACEMENT_REPORT.md`

## 5. Database migrations

Migration `0007_canonical_intelligence` is additive and idempotent. It creates:

- `representation_records`
- `channel_policy_rules`
- `intelligence_retrieval_runs`
- `retrieval_evidence_records`
- `caption_slates`
- `caption_candidate_records`
- `caption_exposures`
- `pairwise_preferences`
- `feedback_signals`
- `image_generation_runs`
- `generated_asset_lineage`
- `intelligence_experiments`

`proposals.caption_slate_id` was added as a nullable, indexed foreign key to
`caption_slates.id`. Candidate records include explicit eligibility, exclusion reasons, attempt
number, and generation order so excluded and retry candidates are not lost.

The populated production database was transactionally backed up before repair:

- backup:
  `data/qlob-production/backups/runway-pre-0007-repair-20260718.db`
- SHA-256:
  `7d785933990210ffe48061d3dce76b913d2f5810a18a8074f0ae04f2b4c206ed`
- before: `0006_uncapped_lineup`, integrity `ok`
- after: `0007_canonical_intelligence`, integrity `ok`
- changed content counts: none
- proposal foreign key/index: present

The migration passed clean-database, populated-upgrade, downgrade, and re-upgrade tests. Legacy
media-vector fields and `caption_feedback` remain readable compatibility inputs; canonical
services write normalized evidence. Existing source, proposal, publisher, and audit rows were not
rewritten.

## 6. Intelligence behavior

### Representations

Explicit image, text, multimodal, and multi-vector providers produce versioned,
content/configuration-hashed records. The store is idempotent and permits model versions to
coexist. Provider lookup fails closed; it never picks an unrequested remote or paid provider.

### Profiles

The profile separates long-term Channel DNA, recent Editorial Mode, explicit Channel Policy,
learned Creator Preference, and Audience Performance. Explicit policy has highest precedence,
and reliability reports sample size and missing evidence.

### Retrieval

Independent pools cover visual, lexical, semantic, multimodal, entity, topic, action, emotion,
scene, composition, caption structure, approval/edit/rejection feedback, recent/scheduled
rotation, image feedback, pairing feedback, and explicit policy. Reciprocal-rank fusion,
role quotas, and MMR produce a bounded diverse package. Every considered, excluded, selected, and
ranked evidence row is persisted.

### Composed retrieval

Reference image plus modification text, desired/excluded entities, topic, action, scene, emotion,
composition, format, overlay preference, aspect ratio, source policy, and rights policy are fused
offline. This is deterministic composed retrieval, not a claimed trained composition model.

### Planning, generation, verification, and ranking

A typed editorial brief defines goal, evidence, structure mix, constraints, uncertainty, and
policy. Generation produces a complete candidate pool without injecting a privileged template.
The independent verifier blocks unsupported entities, events, quotes, relationships, low
confidence facts, historical duplicates, and policy failures. Ranking combines grounding,
policy, preference, pairing, feedback, novelty, rotation, style, and generic penalties. Only
eligible candidates may be displayed. Failure triggers one revised retry and then a typed
abstention; filler captions are forbidden.

### Feedback and preference learning

Every ordered display becomes an exposure. Selection creates preferred-over-unselected pairs;
editing creates edited-over-original pairs. Image, caption, and pairing feedback are independent,
channel-scoped targets. Timestamp decay replaces ID-based recency. Explicit rules, edits,
selections, accepts, and reasoned rejections have documented precedence. Image feedback now
influences discovery.

### Image-generation boundary

The interface declares text, reference, reference-plus-text, multi-reference, and creator-owned
editing capabilities. Unknown rights and missing private-person consent block eligibility. The
only enabled provider is a deterministic offline mock. Output lineage, safety, rights, hashes,
provider identity, and re-entry into candidate storage are persisted.

## 7. Frozen benchmark

- Baseline commit: `876fe5f814b1a58f0d11b9eaf29f4c508f20595d`
- Baseline artifact: `runway-frozen-intelligence-baseline-v1`
- Schema: `0006_uncapped_lineup`
- Profile schema: `style-profile-v2`
- Annotation: `historical-annotation-v2`
- Caption prompt: `captions-v3`
- Cases: 8 total; 3 development, 3 tuning, 2 locked holdout
- Runtime: deterministic mock, publishing disabled, paid calls 0
- Configuration hash:
  `7302e247771c206804e73199a1e288fb651d9140f891b3c59bdf353e222c825e`
- Baseline unique recommendation rate: 0.25
- Baseline unique displayed-caption rate: 0.166667
- Baseline selected-grounding flag rate: 0.333333
- Baseline abstention rate: 0

`runway intelligence baseline` recomputed every saved checksum and returned
`checksum_failures: []`. Artifacts are under
`benchmarks/intelligence/baseline-876fe5f/`.

## 8. Experiments

The bounded tuning protocol used seed `20260718`, one fixed candidate-pool hash, tuning cases only,
no model calls, and a budget of six configurations. It tested the hypothesis that independently
weighting grounding more strongly would improve release safety while preserving policy,
preference, novelty, rotation, style, feedback, and pairing components.

All six final configurations tied at objective 1.0 on the three-case tuning proxy. The winner was
therefore selected by the documented safety-first tie-break: objective first, then higher
grounding weight, then configuration hash. This is not evidence that the winner is subjectively
better than every tied configuration.

Full-engine-minus-component ablations removed grounding, preference, style, novelty, rotation,
positive feedback, negative-feedback risk, and pairing. Every objective delta was 0.0 on the
tiny tuning fixture. The result is inconclusive sensitivity, not evidence that those components
are unnecessary, so none were removed on that basis.

All accepted, rejected, and earlier protocol experiment artifacts remain in the ledger. No failed
or rejected experiment was deleted. The production runtime does not expose those variants.

## 9. Old-versus-new results

### Development

| Metric | Frozen baseline | Canonical | Judgment |
| --- | ---: | ---: | --- |
| Unique recommendations | 0.25 | 0.666667 | Win |
| Unique displayed captions | 0.166667 | 0.555556 | Win |
| Selected/candidate grounding | 0.333333 flag rate | 1.0 pass rate | Win, definitions differ |
| Unsupported claim rate | not available | 0.0 | Canonical passes |
| Mean evidence-role coverage | not available | 1.666667 | Canonical evidence |
| Abstention rate | 0.0 | 0.0 | Tie on grounded fixtures |
| Mean local latency | 69.751 ms | 149.604 ms | Loss, +79.853 ms |

On three identical development IDs, two recommendations were textually tied and one improved from
the unsupported emotional choice “confident” to the visible-evidence term “determined.” Display
slates became structurally grounded instead of using two generic ungrounded fillers.

### Locked holdout

| Metric | Frozen baseline | Canonical |
| --- | ---: | ---: |
| Deterministic no-edit acceptance proxy | 0/2 | 2/2 |
| Pairwise correctness proxy | 0/2 | 2/2 |
| Wilson 95% | `[0, 0.65762]` | `[0.34238, 1]` |
| Grounding pass rate | not directly comparable | 1.0 |
| Unsupported claim rate | not directly comparable | 0.0 |
| Retrieval role coverage | not available | 2.0 |
| Mean local latency | 69.385 ms | 144.071 ms |
| Model calls while scoring | 0 | 0 |
| Estimated provider cost | $0 | $0 |

The observed primary proxy gain is +100 percentage points, but `n=2` produces wide overlapping
intervals. It is not statistically conclusive and is not blind creator preference. No human
preference result exists yet. No model-generated image quality comparison exists because only the
mock lineage path was in scope.

Latency lost in relative terms (2.08x) but increased by only 74.686 ms in absolute local overhead.
No threshold was preregistered. The release judgment treats that absolute cost as operationally
immaterial beside model execution, while retaining the relative regression as a risk.

## 10. Replacement-gate results

### Hard gates

| Gate | Result |
| --- | --- |
| Zero channel-data leakage | Pass |
| Zero migration corruption | Pass |
| Zero publishing-safety regression | Pass |
| Zero hidden paid-provider fallback | Pass |
| Zero rights-policy regression | Pass |
| Zero duplicate-safety regression | Pass |
| Zero unsupported critical entity claims | Pass |
| Required deterministic workflows | Pass |
| Required rollback procedures | Pass |

### Critical regressions

| Case | Result |
| --- | --- |
| Wrong-character prevention | Pass |
| Invented-plot prevention | Pass |
| Invented-quote prevention | Pass |
| Duplicate rejection | Pass |
| Rights blocking | Pass |
| Channel isolation | Pass |
| Explicit-rule enforcement | Pass |
| Abstention without a grounded caption | Pass |

The exact test identifiers and evidence paths are in
`benchmarks/intelligence/experiments/replacement-gates.json`. The fail-closed release suite
contains 51 tests, zero failures, zero errors, and zero skips.

## 11. Winning configuration

- Experiment: `exp-99c73932eeb7`
- Configuration hash:
  `055bfd2a80ef67c60754fcccc1b4cc9b185e88942849cf5e98b4951f2bb1346a`
- Dataset: `canonical-v1`, tuning split
- Candidate-pool hash:
  `a1edce89a909ccc6884f254de5930efe2f807c47cb40bd8fdec16e71949540ce`
- Seed: `20260718`
- Protocol: `bounded-tuning-v2`

Weights:

| Component | Weight |
| --- | ---: |
| Grounding | 0.28 |
| Preference | 0.20 |
| Policy | 0.12 |
| Novelty | 0.09 |
| Pairing | 0.09 |
| Negative feedback risk | -0.08 |
| Style | 0.07 |
| Positive feedback | 0.06 |
| Rotation | 0.06 |
| Generic penalty | -0.12 |

The winner tied every other tested configuration at objective 1.0 and was selected by the
safety-first grounding tie-break. The one-time holdout returned objective 1.0, grounding 1.0,
unsupported claims 0, no-edit proxy 1.0, and pairwise proxy 1.0. Its sample is too small for a
human-quality or statistical-superiority claim.

## 12. Canonical cutover

- The improved behavior is in the normal analysis, profile, retrieval, discovery, caption,
  proposal, and feedback services.
- Temporary experiment versions are artifacts, not runtime service choices.
- No `_legacy`, `_shadow`, or runtime intelligence V1/V2 selector remains.
- Qlob question-first behavior is explicit channel policy.
- Dead entity/emotion template helpers and their privileged ranking path were removed.
- Generic fillers were replaced by bounded retry plus typed abstention.
- Legacy database fields remain only where existing records/APIs require compatibility.

Rollback:

1. Stop RunWay services and disable publishing.
2. Preserve the SQLite database transactionally, including WAL state.
3. Prefer restoring the verified pre-migration backup for exact recovery.
4. If new intelligence evidence may be discarded, run
   `alembic downgrade 0006_uncapped_lineup`.
5. Revert the canonical upgrade commit or restore baseline commit `876fe5f...`.
6. Run `runway doctor`, database integrity checks, and fixture tests before restarting.

The downgrade path was tested on populated data. No automatic destructive rollback command was
added.

## 13. Architect-initiated improvements

### Label provenance

Automated fixture rules, policy proxies, and future human labels are reported separately. This
prevents a deterministic score from being presented as creator preference.

### Content-cluster split guard

Duplicate/source/sequence clusters cannot cross development, tuning, and holdout partitions.
Blind pair order is seeded and reproducible.

### Typed abstention

The engine retries once and then returns a typed abstention. It cannot present generic filler as a
successful slate.

### Complete retry provenance

Every candidate from every attempt is persisted, including failed, duplicate, excluded, and
undisplayed rows. Eligibility is explicit rather than inferred from rank.

### Test data isolation

Test environment variables are set before API imports, so test collection cannot initialize or
migrate the configured production database.

### Production migration recovery

The partial SQLite DDL state was made idempotently repairable, backed up transactionally, and
verified without changing content counts.

Deliberately not added: heavy model downloads, vector-server infrastructure, remote image
generation, live publishing tests, model fine-tuning, public multi-tenancy, or a permanent
comparison engine. None had sufficient evidence-to-risk value for this release.

## 14. Testing

| Command/check | Scope | Result | Why |
| --- | --- | --- | --- |
| `.\.venv\Scripts\python.exe -m pytest -q` | Complete backend suite | 90 passed, 1 existing deprecation warning | Repository-wide service/schema blast radius |
| Critical pytest list with `--junitxml=benchmarks\intelligence\experiments\critical-gates.xml -q` | Migration, isolation, grounding, duplicate, rights, provider, publishing, E2E gates | 51 passed, 1 existing warning | Machine-readable release proof |
| Focused intelligence suite | New foundations and integrations | 26 passed | Fast implementation feedback |
| Strict schema/runtime subset | Model schemas and Codex boundary | 25 passed | Structured-output compatibility |
| `ruff check src tests` | Python lint | Passed | Broad Python changes |
| `.\.venv\Scripts\python.exe -m mypy src` | 83 source files, strict project config | Passed, no issues | New typed boundaries and models |
| `npm run check` | ESLint, Vitest, TypeScript, optimized Next.js build | 9 files / 51 tests passed; build passed | Shared API/product integration and required frontend gate |
| `.\.venv\Scripts\runway.exe intelligence baseline` | Frozen artifact hashes and manifest | Verified, zero checksum failures | Immutable baseline proof |
| `.\.venv\Scripts\runway.exe --help` | Installed CLI surface | Passed | New command registration |
| `.\.venv\Scripts\python.exe -m pytest tests\unit\test_cli_health.py -q` | Focused final CLI regression | 2 passed | Verify final CLI wording/registration adjustment |
| `git diff --check` | Patch whitespace/integrity | Passed | Final repository hygiene |
| SQLite backup + read-only integrity/schema/count queries | Populated production DB | `ok`; zero content-count changes | Preserve real data |

The Starlette/httpx deprecation warning predates this upgrade and is non-failing.
The direct `mypy.exe` Windows launcher exited once without diagnostics; invoking the same installed
module through the virtual-environment Python completed successfully with strict settings.

Checks deliberately not run:

- Live YouTube publication: prohibited by the task and not an intelligence validation.
- Paid OpenAI or image-generation calls: prohibited and unnecessary.
- Model or embedding downloads: prohibited and unnecessary.
- GPU tests: no GPU path is required.
- Dependency audit/reinstallation: no required dependency was added.
- Real image-generation quality test: no real provider was enabled.
- Blind human preference import: the creator has not supplied a blinded label set.

Skipped checks are not counted as passed.

## 15. Experimental or future work

- Optional evaluated SigLIP/DINO/VLM representation adapters with explicit licences and resource
  budgets.
- Multi-vector late-interaction retrieval beyond the deterministic provider.
- Learned reference-image-plus-text composition after a labelled retrieval set exists.
- Real image-generation/edit providers behind the existing rights, consent, cost, and lineage
  gates.
- Caption fine-tuning only after enough high-quality creator comparisons exist.
- Audience-performance learning kept separate from creator preference.
- Calibrated approval probability and decision-time modelling.
- Cold-start profile priors for channels with little history.

These are architecturally prepared or explicitly bounded; they are not production capabilities.

## 16. Known limitations

- The locked holdout has only two cases.
- Human blind preference, real edit distance, and real decision time are unmeasured.
- All six tuning configurations tied; the chosen weights are a safety tie-break, not a proven
  optimum.
- Ablations were insensitive on three tuning cases.
- Deterministic local vectors are not neural semantic embeddings.
- Existing Qlob records retain older annotations until incremental re-analysis.
- Preference learning remains in fallback mode until pairwise evidence grows.
- Real Codex latency, usage variability, and output quality were not measured in the offline
  benchmark.
- Mock image generation proves boundaries and lineage, not image quality.
- Historical benchmark metrics use some definitions that are not directly comparable to new
  verifier metrics.

## 17. Risks

- Sparse labels can overstate apparent gains and conceal channel-specific failure modes.
- Stricter grounding can increase abstention on ambiguous real images.
- Relative local latency doubled even though absolute overhead remains small.
- Browser-based discovery and YouTube publishing remain sensitive to external UI changes.
- Incremental annotation versions can create temporary mixed-version evidence.
- Rights metadata from external sources may be incomplete; unknown remains blocked/review-only.
- A future provider adapter could violate cost/privacy assumptions if registry and policy tests
  are bypassed.

## 18. Active-learning plan

Labels still needed:

- blind baseline-versus-canonical caption preference;
- accept-without-edit versus edited versus rejected;
- edit distance and decision latency;
- wrong entity/emotion/action corrections;
- image-only rejection reasons;
- caption-only rejection reasons;
- pairing failures;
- retrieval relevance for each evidence role;
- abstention correctness;
- real-channel cases outside animation/reaction content.

Selection priority:

1. close top-two preference scores;
2. verifier uncertainty or low field confidence;
3. abstentions;
4. disagreement between visual and text retrieval;
5. repeated edit/rejection clusters;
6. channel-policy boundary cases;
7. new entities, languages, formats, and multi-image posts.

These cases have the highest expected information value because they distinguish ranking,
grounding, retrieval, and policy failures rather than merely adding easy accepted examples.
`runway intelligence active-learning` produces a deterministic review queue.

## 19. Recommended next milestone

Run a preregistered blind creator-preference study on at least 50 untouched, channel-representative
image cases. Randomize baseline/canonical side order, include tie and neither options, record
decision time and edits, and keep duplicate/content clusters together. Do not tune on this set.

Expected outcome: determine whether the deterministic grounding/diversity gains translate into
creator preference and lower editing effort.

Required evidence:

- configuration and dataset hashes fixed before labels;
- at least 50 valid blinded decisions;
- win/loss/tie counts and Wilson interval;
- no-edit and edit-distance deltas;
- subgroup results for ambiguity, multi-image, entity confidence, and retrieval role;
- all current hard/critical gates still passing;
- a preregistered latency and model-usage budget.

Implementation scope: export through the existing blind-review command, collect creator choices,
import the immutable responses, compute the paired report, and use only a later separate tuning
split for any response. This is the highest-value next experiment because human preference is the
largest remaining unproven claim.
