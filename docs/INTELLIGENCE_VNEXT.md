# Canonical intelligence engine

## Architectural assessment

Baseline authority is commit
`876fe5f814b1a58f0d11b9eaf29f4c508f20595d`, schema
`0006_uncapped_lineup`, profile schema `style-profile-v2`, historical annotation
`historical-annotation-v2`, and caption prompt `captions-v3`. Its immutable fixture outputs,
configuration, prompt hashes, metrics, and checksums are under
`benchmarks/intelligence/baseline-876fe5f/`.

The existing implementation has valuable foundations:

- immutable source capture and content-addressed media;
- additive database migration history;
- deterministic fixture, image, duplicate, and local-runtime paths;
- typed model boundaries and immutable raw model outputs;
- append-only corrections, feedback, proposal events, and audit events;
- strong exact and transformed-duplicate safeguards;
- explicit no-paid-fallback and publishing boundaries;
- bounded model context instead of a raw catalogue dump.

The audit confirmed these P0 weaknesses:

- several profile, retrieval, duplicate-caption, feedback, ranking, and discovery queries are
  not channel-scoped;
- visual retrieval uses thumbnail intensity, color histograms, and edge statistics;
- text similarity uses token and bigram hashing rather than a semantic provider boundary;
- `qlob_style_score`, Qlob wording in prompts, franchise-first search planning, and universal
  question-first selection embed one channel's preferences in reusable logic;
- only the first historical image enters retrieval, although model annotation can see multiple
  images;
- active profile lookups are sometimes global;
- retrieval candidates, scores, exclusions, and selections are not first-class evidence;
- caption generation stores a raw pool in `model_runs`, but displayed slates, individual
  candidates, exposures, and pairwise choices are not first-class records;
- fixed ranking coefficients are not learned preference probabilities;
- one injected `Why is [entity] so [emotion]?` candidate receives privileged selection logic;
- generic fallback captions can be displayed instead of a typed abstention;
- candidate confidence is global rather than field-level;
- image feedback is co-located with caption feedback and does not materially drive discovery;
- explicit policy, long-term channel DNA, recent editorial mode, learned creator preference, and
  audience performance are not separate layers;
- unknown rights are mostly a warning in image ranking rather than an explicit policy outcome;
- the OpenAI caption adapter loads `captions-v2.txt` while the service records `captions-v3`;
- similarity-edge rebuild deletes every channel's edges;
- feedback recency contains an ID-based tie-break contribution.

The prompt's hypotheses were therefore confirmed except for two revisions:

- complete caption pools do exist inside immutable `model_runs`; the missing evidence is
  normalized candidate, display, exposure, and pairwise-decision data;
- multi-image historical annotation is supported, but historical retrieval and similarity
  collapse the post to its first image.

The specification omits one important control: a release decision must distinguish human labels
from deterministic policy labels. Synthetic fixtures can prove isolation, adaptation, safety,
and reproducibility, but cannot be represented as blind creator preference. Human preference
remains a separately reported metric.

## Priority and scope

### P0

- channel ownership and query isolation;
- additive evidence, representation, policy, slate, preference, generation-lineage, and
  experiment records;
- frozen old-engine benchmark;
- deterministic provider registry with no implicit remote fallback;
- hybrid retrieval with persisted considered and selected evidence;
- profile layers and explicit-policy precedence;
- editorial brief, grounding verifier, common caption taxonomy, diversity, and abstention;
- separate image, caption, and pairing feedback;
- safe generation eligibility and deterministic mock generation;
- deterministic development, tuning, holdout, generalization, and critical-regression datasets;
- replacement gates, rollback proof, and one canonical runtime path.

### P1

- dependency-light pairwise preference model when enough comparisons exist;
- reference-image-plus-text retrieval;
- multi-image pooled and per-image representations;
- deterministic bounded tuning, ablation, confidence intervals, and active-learning selection;
- image-feedback features in image ranking.

### P2

- optional SigLIP/DINO/VLM or multi-vector adapters;
- learned composed-image retrieval;
- real image generation or editing;
- language-model fine-tuning;
- audience-performance optimization;
- calibrated approval probabilities;
- cold-start optimization.

P2 items are not placed on the default path because no weights may be downloaded, no paid
provider may be invoked, and the present evidence cannot measure their incremental value.

## Final canonical data flow

```text
channel history + explicit rules + recent state + editorial feedback
                              |
                              v
                     layered channel profile
                              |
reference image + instruction + constraints + rights policy
                              |
                              v
versioned representations -> independent retrieval pools
                              |
                              v
                  deterministic fusion + MMR
                              |
                              v
        persisted role-based evidence package and editorial brief
                              |
                              v
           caption pool -> verifier -> preference ranker
                              |
                              v
              diverse display slate or typed abstention
                              |
                              v
 creator exposure -> selection/edit/accept/reject -> separate feedback
                              |
                              v
     pairwise preferences + image/caption/pairing learning evidence
```

Normal product paths call one `RetrievalService`, one `CaptionService`, one
`CandidateRanker`, and one provider registry. Saved baseline artifacts replace the need for a
permanent old runtime.

## Evidence and data model

The additive schema stores:

- versioned entity representations with content and configuration hashes;
- channel policy rules with priority, validity, source, and retirement state;
- retrieval runs and every considered evidence item;
- caption slates, all generated candidates, and ordered creator exposures;
- pairwise preferences derived from selections and edits;
- independent image, caption, and pairing feedback signals;
- image-generation runs and generated-asset lineage;
- immutable intelligence experiment records.

Legacy media vectors and `caption_feedback` remain compatibility inputs. Canonical services write
the new normalized records and may mirror the old record where an existing API depends on it.
No production source record or raw model output is overwritten.

## Representation strategy

`RepresentationProviderRegistry` resolves an explicitly configured local provider by capability
and never falls back. Every result identifies provider, model, version, purpose, dimensions,
vector count, normalization, and configuration hash.

The default deterministic providers are dependency-light:

- exact and transformed duplicate representations retain SHA-256, perceptual hashes, and crop
  descriptors;
- local image semantics reuse the current numeric descriptor but store it through the versioned
  representation contract;
- local text semantics combine signed word, bigram, character-ngram, and canonical-concept
  features;
- a multimodal provider fuses normalized image and text vectors;
- pooled post records preserve every image and a post-level mean instead of selecting only
  position zero.

These providers are reproducible offline baselines, not trained semantic models. Optional heavy
adapters must declare licence, resource, latency, privacy, model, migration, and backfill details;
they do not load or download weights by default.

## Profile and policy strategy

Each channel profile has distinct layers:

1. long-term Channel DNA;
2. recent Editorial Mode;
3. explicit Channel Policy;
4. learned Creator Preference;
5. separately reported Audience Performance.

Explicit policy outranks inferred behavior. A Qlob question-first rule is channel data seeded
from Qlob configuration, not a universal caption rule. Profile reliability reports sample size
and missing evidence. Language and locale are schema fields even when deterministic fixtures are
English.

## Retrieval strategy

Independent pools include visual similarity, image-text alignment, lexical and semantic text,
entities, topics, actions, emotions, scenes, compositions, caption structure, approvals, edits,
rejections, image feedback, pairing feedback, recent content, scheduled content, and explicit
rules. Reciprocal-rank fusion is deterministic. Role quotas and maximal marginal relevance
prevent one pool, duplicate cluster, caption structure, or repeated wording from dominating.

Every considered item records raw, normalized, fusion, recency, policy, diversity, exclusion,
selection, rank, role, and duplicate-cluster information. The evidence compiler emits a bounded
ordered brief, not a database dump.

Composed retrieval accepts reference media, modification text, desired and excluded entities,
topic, action, scene, emotion, composition, format, overlay preference, source/rights policy,
aspect ratio, and post format. The offline implementation fuses reference-image, text, structured
constraint, and policy scores; it is not described as a trained composed-retrieval model.

## Caption pipeline

The canonical pipeline is:

1. compile evidence;
2. build a typed editorial brief;
3. generate a profile-driven candidate pool;
4. run deterministic grounding and policy verification;
5. reject historical and near duplicates;
6. rank with explicit components and a pairwise preference model when evidence is sufficient;
7. select a structurally and lexically diverse display slate;
8. retry once with a revised brief or return a typed abstention.

One canonical caption taxonomy is shared by generation, feedback, ranking, and evaluation.
Templates receive no rank privilege. Every candidate retains its own evidence, verifier result,
confidence, and rank components.

## Preference and feedback strategy

The system records the full ordered display. Selection creates preferred-over-each-unselected
pairs. Editing creates edited-over-original pairs. Accept, reject, image replacement, reasons,
notes, and decision latency are channel-scoped evidence.

Feedback targets are independent:

- image;
- caption;
- image-caption pairing.

Time decay uses timestamps. Explicit rules have highest precedence, followed by edits,
selections, accepts, reasoned rejections, unreasoned rejections, and unselected exposures.
Audience performance remains a separate target.

## Safe image-generation boundary

`ImageGenerationProvider` reports supported capabilities for text-to-image, reference-image,
reference-plus-text, multi-reference, and creator-owned editing. The only default provider is a
deterministic offline mock. Real providers are explicitly configured, disabled by default, and
never called by tests.

Generation eligibility allows creator-owned, licensed, public-domain, or explicitly approved
references. Unknown-rights references cannot silently become generation inputs. Private-person
identity preservation requires consent. Generated outputs re-enter content-addressed storage,
duplicate, safety, rights, annotation, ranking, and human-review stages.

## Evaluation and tuning design

Datasets are versioned into development, tuning, locked holdout, blind human preference,
cross-channel generalization, and critical regression partitions. Duplicate/source/sequence
clusters cannot cross splits. Tuning code receives only development and tuning labels. Holdout
access is recorded and occurs once for the selected release candidate.

The runner reports paired case outputs, retrieval metrics, role coverage, grounding, unsupported
claims, caption diversity, preference accuracy, failures, abstentions, latency, model use, and
cost. A seeded bounded search preserves accepted and rejected experiments. Ablations isolate
retrieval fusion, diversity, planning, verification, preference learning, negative feedback,
image feedback, and pairing. Wilson intervals are computed without a statistics dependency.

Replacement requires every safety, isolation, migration, duplicate, grounding, no-paid-fallback,
publishing, and rollback gate, all critical cases, all cross-channel fixtures, meaningful
improvement in a declared primary metric, and no material secondary regression.

## Cross-channel generalization

Offline histories cover:

- short animation/meme reactions;
- sports discussion;
- educational science;
- gaming;
- product/brand content.

They differ in language signals, length, structures, vocabulary, entities, visuals, policy,
positive examples, and negative examples. Tests require independent profiles, isolated evidence,
non-universal question-first behavior, policy precedence, and different recommendations for the
same input.

## Migration and rollback

The migration is additive and idempotent against the repository's `0001` metadata-first pattern.
It is tested from an empty database and from a copy at `0006_uncapped_lineup`. Downgrade removes
only new intelligence tables/columns. Production data is never rewritten.

Rollback procedure:

1. stop local Runway services;
2. copy `runway.db`, `runway.db-wal`, and `runway.db-shm` as one backup set when present;
3. run `alembic downgrade 0006_uncapped_lineup` only if new intelligence records may be discarded;
4. check out baseline commit `876fe5f...` or revert the upgrade commit;
5. restart with publishing disabled and run `runway doctor`;
6. restore the database backup instead of downgrading if exact experiment history is required.

No automated rollback command deletes production data.

## Privacy, rights, security, latency, and cost

All default intelligence remains local. Secret settings and credentials are excluded from
records, reports, and cache keys. Remote URLs remain untrusted; hosted deployments still require
private-network blocking, redirect validation, streaming byte limits, decompression-bomb
protection, and sandboxed parsing.

Rights state and source provenance are preserved. Unknown rights are never represented as safe.
Generation eligibility is stricter than ordinary editorial review.

The SQLite/NumPy default uses bounded brute-force retrieval because the local catalogue is small.
Every stage records elapsed time, cache identity, calls, tokens, failures, and estimated cost.
Default offline cost is zero. No new required dependency is introduced.

## Non-goals

- public signup, billing, agency workspaces, or a multi-tenant UI;
- hidden scraping or challenge bypass;
- live YouTube publication;
- model download, GPU requirement, or language-model fine-tuning;
- enabling a real image-generation provider;
- claiming calibration, human preference, or semantic-model quality without evidence.

## Architect-Initiated Improvements

### Label provenance

- Issue: automated, policy, synthetic, and human labels could otherwise be combined into one
  misleading preference metric.
- Evidence: the baseline has no blind creator comparison set.
- Solution: record label source and report human preference separately.
- Alternative: treat deterministic fixture rules as creator preference; rejected as misleading.
- Cost: small schema and reporting fields.
- Expected benefit: valid release claims.
- Remaining uncertainty: more creator labels are still required.

### Content-cluster split guard

- Issue: transformed duplicates and recurring variants can leak across evaluation partitions.
- Evidence: the production catalogue contains exact and near-duplicate clusters.
- Solution: deterministic cluster ownership and split validation.
- Alternative: random row split; rejected due leakage.
- Cost: one dataset validator.
- Expected benefit: more trustworthy holdout results.

### Typed abstention

- Issue: generic fallback text can conceal a failed generation pass.
- Evidence: the baseline always fills a display slate and reports no abstentions.
- Solution: one retry followed by an explicit typed abstention.
- Alternative: keep generic fillers; rejected because it creates false success.
- Cost: small service/API behavior.
- Expected benefit: fewer unsupported or low-value suggestions.

## Risks

- deterministic local vectors are still weaker than evaluated semantic vision-language models;
- sparse creator comparisons can leave the preference model in fallback mode;
- existing Qlob annotations use the older schema until incrementally re-annotated;
- synthetic generalization proves isolation and adaptation, not real audience performance;
- broad schema changes increase migration and query-audit blast radius;
- stricter verification can initially increase abstention.

## Promotion criteria

The final experiment report must identify the winning configuration hash, tuning budget,
ablation results, one-time holdout result, every hard gate, every critical regression case, and
uncertainty. Canonical cutover is blocked if any required gate fails. The repository must end
without a runtime V1/V2 selector or duplicate production intelligence path.

## Final promotion result

The canonical replacement was promoted after the following saved evidence:

- the immutable baseline at commit `876fe5f814b1a58f0d11b9eaf29f4c508f20595d`
  verified with zero checksum failures;
- a bounded six-configuration tuning run selected configuration
  `055bfd2a80ef67c60754fcccc1b4cc9b185e88942849cf5e98b4951f2bb1346a`;
- the locked holdout was opened exactly once and improved deterministic no-edit and
  pairwise-policy proxies from 0/2 to 2/2;
- all five same-image/different-channel generalization fixtures passed with five distinct,
  policy-conforming captions and no channel leakage;
- all nine hard gates and all eight critical regression categories passed;
- the critical release suite passed 51/51 tests, and the complete backend suite passed
  90/90 tests;
- production schema migration from `0006_uncapped_lineup` to
  `0007_canonical_intelligence` preserved every recorded content count and both pre/post
  integrity checks returned `ok`;
- no live publication, paid provider call, model download, or GPU execution occurred.

The observed two-case holdout gain is large but not statistically conclusive: baseline Wilson
95% is `[0.00000, 0.65762]` and canonical Wilson 95% is `[0.34238, 1.00000]`. It is deterministic
policy-proxy evidence, not blind human preference. All six bounded tuning configurations tied on
the tiny tuning objective; the selected configuration won the preregistered safety-first
tie-break by assigning the largest tested independent grounding weight. Ablations also tied and
are therefore inconclusive, not evidence that components are unnecessary.

Local deterministic holdout latency increased from 69.385 ms to 144.071 ms. The +74.686 ms
absolute overhead was judged operationally immaterial beside model execution, but the 2.08x
relative regression is retained as a risk because no latency threshold was preregistered and real
Codex latency was not measured.

The exact evidence and direct technical judgment are in
`docs/INTELLIGENCE_REPLACEMENT_REPORT.md` and
`benchmarks/intelligence/experiments/replacement-gates.json`.
