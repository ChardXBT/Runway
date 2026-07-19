# Qlob production validation

Validated on 2026-07-17 against the ignored local production directory
`data/qlob-production/`. No automated test or browser visual-QA pass clicked a YouTube Post or
Schedule control.

## Historical catalogue

- Capture run `1` reached a stable bottom with the `youtube-community-v3` adapter.
- The surface exposed 771 unique post IDs, 771 normalized posts, 771 raw provenance records, and
  771 immutable DOM snapshots.
- There are 732 post-to-media links, 716 unique downloaded historical media files, and 698
  image+caption records eligible for visual annotation.
- Verification found zero missing captions, missing images, broken files, duplicate external post
  IDs, unmatched raw records, capture errors, or non-fatal diagnostics.
- Fifteen exact duplicate-media groups and 34 near-duplicate clusters were preserved and labeled.
- YouTube exposed relative publication labels; Runway preserved `relative` precision instead of
  inventing source timestamps.

Evidence:

- `data/qlob-production/reports/catalog-verification.json`
- `data/qlob-production/reports/catalog-verification.md`

## Codex analysis and retrieval

The production runtime used saved ChatGPT authentication, `gpt-5.6-luna`, low reasoning, and no
OpenAI API key or paid fallback.

- 140 resumable batches produced 698 `historical-annotation-v2` annotations.
- Runway built all 243,253 unique pairwise similarity edges for 698 posts.
- Seven uncertainty records received correction overlays; original model outputs remain immutable.
- Style profile v4 uses 558 training records and a deterministic 140-record holdout.
- The training profile has a median caption length of five words and 26 characters.

| Measure | Result |
| --- | ---: |
| Exact historical image-to-caption recovery | 3/140 (2.14%) |
| Qlob caption ranking | 35/140 (25%) |
| Top-three franchise retrieval relevance | 135/140 (96.43%) |
| Controlled transformed-duplicate recall | 100% |
| Controlled unrelated false-positive rate | 0% |

Exact caption recovery is weak, so generated captions remain reviewable suggestions. Retrieval,
automatic feedback, and the question-first reranker improve choices without claiming that one
caption is objectively correct.

Evidence:

- `data/qlob-production/reports/style-profile-v4.json`
- `data/qlob-production/reports/style-profile-v4.md`
- `data/qlob-production/reports/profile-evaluation.json`
- `data/qlob-production/reports/profile-evaluation.md`

## Discovery safeguard pass

A deliberately bounded headed Bing Images pass downloaded six candidates after Google showed a
challenge that Runway did not bypass.

- Three candidates passed the hard automated filters.
- One historical duplicate and two prominent-watermark results were rejected.
- The marketplace/personal-artwork/fan-art candidate was hard rejected.
- Page URL, direct-image URL, and `rights_status=unknown` were retained for every candidate.

The current Review conveyor does not ask for a copyright or provenance declaration. Source data
remains diagnostic, and an explicitly blocked candidate still cannot be approved or scheduled.

## Real proposal and current workflow

Generation run `2` created proposal `1` from a Homer Simpson reaction still. The operator changed
the caption to `Why is Homer so excited` and approved it for the next 10:00 AM Toronto slot. It was
stored as internally scheduled; it has no external YouTube post ID.

The original dense review surface was subsequently replaced by:

- one dominant image/caption decision at a time;
- inline caption editing;
- the focused `Reject`, `Edit`, and `Accept` controls;
- immediate next-option loading and automatic tray refill;
- append-only positive/negative learning from every decision;
- a persisted serial YouTube outbox; and
- no fixed horizon, with at most one Runway-created post per Toronto local date.

The source, warning, model rationale, and activity records remain available as evidence without
occupying the primary approval flow.

## Automated quality gate

- Pytest: 51 passed.
- Ruff lint/format: passed.
- Mypy strict mode: passed.
- ESLint: passed.
- Vitest: six tests passed.
- Next.js production build and TypeScript: passed.
- Root and web npm audits: zero known vulnerabilities.

## External acceptance boundary

The software and offline browser contract are complete. A real Qlob Schedule click is deliberately
excluded from routine tests and visual QA. The first operator click on `Accept` with
publishing enabled is the proposal-specific authorization for that exact image, caption, and
allocated time. Runway must then verify the matching Scheduled item or stop in a verification-only
state; it must never infer success or submit a duplicate.
