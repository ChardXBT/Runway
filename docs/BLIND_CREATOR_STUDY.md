# RunWay blind creator study

This procedure measures creator preference without exposing which caption,
image, or pairing came from the baseline or challenger. The tooling can prepare,
blind, export, import, and report a study; it cannot manufacture human judgment.

No production model or representation should be activated from a study until
the preregistered sample is complete and the relevant hard safety gates pass.

## Purpose

The primary outcome is direct creator pairwise preference. Secondary outcomes
may include:

- whether either or both options are acceptable;
- edit rate and edit distance;
- image, caption, and pairing verdicts;
- decision time;
- reason-code clusters;
- ties and abstentions.

A public benchmark, automated score, or synthetic fixture is not a substitute
for this study.

## Protections

- At least 50 cases are required by the default export gate.
- Each case uses unique channel-owned media.
- A perceptual duplicate cluster contributes at most two cases.
- Related examples share a `group_key` and cannot cross splits.
- Baseline/challenger position is randomized deterministically from the
  preregistered seed.
- The review export removes origin, provider, model, rank, score,
  configuration, baseline, and challenger metadata.
- Final-holdout cases must be untouched by tuning and active learning.
- Import requires an explicit reviewer label and review-session identity.
- A case can receive only one response; duplicates are rejected.
- Imported creator responses are stored with `label_source=human`.
- The report remains descriptive until an appropriate confidence analysis is
  performed.

## Step 1: freeze the comparison

Before selecting cases, record:

- baseline and challenger identities;
- target: `caption`, `image`, or `pairing`;
- exact provider/model/prompt/representation/ranker versions;
- profile, policy, and retrieval configuration;
- seed;
- case-selection rules;
- primary and secondary outcomes;
- safety and activation gates.

Do not change either system after cases are generated. A changed artifact needs
a new study identity.

## Step 2: build the case plan

The plan is a JSON object with a `cases` list. Each entry has this shape:

```json
{
  "case_key": "qlob-caption-001",
  "media_asset_id": 123,
  "candidate_image_id": 45,
  "group_key": "candidate:45",
  "split": "final_holdout",
  "baseline_caption": "Baseline caption text",
  "challenger_caption": "Challenger caption text",
  "baseline_candidate_id": 201,
  "challenger_candidate_id": 202,
  "metadata": {
    "content_category": "reaction"
  },
  "selection_rationale": {
    "representative": true,
    "untouched": true
  }
}
```

`candidate_image_id`, `baseline_candidate_id`, and
`challenger_candidate_id` may be omitted when the comparison artifact does not
have those database records, but every media asset must belong to the configured
channel. When candidate IDs are supplied, their media and text must match the
case exactly.

Allowed splits are:

- `development`: workflow validation, never a final claim;
- `tuning`: hypothesis and error-cluster iteration;
- `final_holdout`: untouched final creator comparison.

Use a separate final-holdout plan. Never move cases into the holdout after
observing their outcomes.

## Step 3: preregister and persist

Run with publishing disabled:

```powershell
$env:RUNWAY_DATA_DIR = "data/qlob-production"
$env:RUNWAY_PUBLISHING_ENABLED = "false"
$env:RUNWAY_AGENT_RUNTIME = "mock"

.\.venv\Scripts\runway.exe intelligence study plan `
  --cases data/qlob-production/reports/blind-study-plan.json `
  --baseline-identity "runway-local-caption-baseline-v1" `
  --challenger-identity "candidate-caption-system-v2" `
  --target caption `
  --seed 20260718
```

Planning validates unique case keys, unique media, channel ownership,
candidate/media consistency, candidate/text consistency, group split
protection, and duplicate-cluster limits. The persisted preregistration and case
content hashes make a repeat deterministic.

## Step 4: export the blinded review

```powershell
.\.venv\Scripts\runway.exe intelligence study export `
  --study-id <id> `
  --output data/qlob-production/reports/blind-study-review.json `
  --minimum-cases 50
```

The command refuses to export fewer than the requested minimum. Inspect the
artifact before review and verify that no field reveals origin or model
identity.

The reviewer should:

1. assess the image before reading both options;
2. choose `first`, `second`, or `tie`;
3. mark either or both options acceptable when appropriate;
4. optionally provide the final caption they would actually use;
5. record image, caption, and pairing verdicts separately;
6. select reason codes and add a note only when useful;
7. avoid researching which system produced an option.

## Step 5: prepare genuine responses

The response artifact is a JSON object with a `responses` list:

```json
{
  "responses": [
    {
      "case_key": "qlob-caption-001",
      "choice": "first",
      "acceptable_choices": ["first", "second"],
      "edited_final_caption": "What would you do if Homer looked at you like this?",
      "image_verdict": "accepted",
      "caption_verdict": "accepted",
      "pairing_verdict": "accepted",
      "reason_codes": ["open_question", "visually_grounded"],
      "note": null,
      "decision_time_ms": 8400,
      "started_at": "2026-07-20T14:00:00Z"
    }
  ]
}
```

Valid choices are `first`, `second`, and `tie`. Verdicts are `accepted`,
`rejected`, or `unsure`. Do not populate decisions from automated rankings.

## Step 6: import once

```powershell
.\.venv\Scripts\runway.exe intelligence study import `
  --study-id <id> `
  --review data/qlob-production/reports/blind-study-responses.json `
  --review-session "qlob-creator-20260720" `
  --reviewer-label "local-creator"
```

An imported non-tie choice creates a target-specific pairwise preference linked
to the exact study response and immutable feature snapshots. Re-importing the
same case is rejected rather than counted twice.

Keep the review artifact private because notes and timing data may be personal.
Runtime report files are ignored by Git.

## Step 7: inspect status and report

```powershell
.\.venv\Scripts\runway.exe intelligence study status --study-id <id>
.\.venv\Scripts\runway.exe intelligence study report --study-id <id>
```

The report provides baseline wins, challenger wins, ties, edits, sample size,
and a truthful statistical-claim field. It reports `synthetic_labels: 0`.

Before making an activation decision, additionally calculate and record:

- sample size and missing responses;
- wins, losses, and ties;
- a confidence interval appropriate to the paired outcome;
- results by preregistered split and content category;
- no-edit acceptance and edit distance;
- decision-time distribution;
- grounding, policy, duplicate, and rights failures;
- material failure clusters;
- latency and resource deltas.

Do not remove inconvenient cases after seeing results. Report exclusions only
when the preregistration specified the rule and preserve the original response.

## Active learning is separate

Active learning selects uncertain and diverse development/tuning examples for
efficient labeling:

```powershell
.\.venv\Scripts\runway.exe intelligence active-learning select `
  --target caption --limit 20 --seed 20260718
```

It must not draw from or contaminate the final holdout. Active-learning labels
may improve later candidates, after which the final comparison must use still
untouched media and groups.

## Current status

As of the 2026-07-19 flywheel migration:

- study and active-learning tooling are implemented and fixture-tested;
- exports support the required 50-case minimum;
- production has no persisted blind study or imported creator response;
- no human preference improvement is claimed;
- completing the study requires the creator's genuine review.
