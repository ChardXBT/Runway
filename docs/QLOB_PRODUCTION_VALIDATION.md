# Qlob production validation

Validated on 2026-07-17 against the local production data directory
`data/qlob-production/`.

This run captured and analyzed the Qlob history exposed by YouTube, exercised bounded live image
discovery, generated one real review proposal, rendered every application page, and verified
restart persistence. It did not approve, schedule, create, or publish a YouTube post. Live
publishing remained disabled for the entire run.

Runtime data, downloaded media, browser state, screenshots, and the SQLite database remain local
under the ignored `data/` tree. They are not committed to GitHub.

## Historical catalogue

- Capture run `1` reached a stable bottom and completed with the
  `youtube-community-v3` adapter.
- The surface exposed 771 unique post IDs, represented by 771 normalized posts, 771 raw provenance
  records, and 771 immutable DOM snapshots.
- The catalogue contains 732 post-to-media links and 716 unique downloaded historical media
  files. Of the 771 posts, 698 are image+caption records eligible for visual annotation.
- YouTube exposed relative publication labels for all 771 posts. LeeWay preserved `relative`
  precision and reports an approximate range from 2024-07-17 through 2026-07-13 instead of
  claiming exact source timestamps.
- Verification found zero missing captions, missing images, broken files, duplicate external
  post IDs, unmatched raw records, capture errors, or non-fatal diagnostics.
- Fifteen exact duplicate-media groups and 34 near-duplicate clusters were retained and identified
  rather than silently discarded. Byte-identical frames within the same gallery are linked once
  and recorded in the audit log.
- A complete dry reparse scanned and matched all 771 immutable snapshots with zero errors.

The local verification evidence is written to:

- `data/qlob-production/reports/catalog-verification.json`
- `data/qlob-production/reports/catalog-verification.md`

## Codex analysis and retrieval profile

The production runtime used saved ChatGPT authentication, `gpt-5.6-luna`, low reasoning, and no
OpenAI API key or paid-API fallback.

- One hundred forty successful resumable batches produced 698
  `historical-annotation-v2` annotations. One strict one-to-one ID-mapping rejection during the
  expansion run committed no partial batch and then passed unchanged on `--resume`.
- The completed historical batches used 1,729,087 input tokens, including 803,840 cached input
  tokens, plus 170,572 output tokens and 15,734 reasoning-output tokens from the included Codex
  allowance.
- A final `--resume` pass skipped all 698 completed records, issued zero annotation batches, and
  left the catalogue unchanged.
- LeeWay built all 243,253 unique pairwise similarity edges for 698 posts.
- Ten uncertainty/outlier records were visually reviewed. Seven records—posts
  `6, 11, 49, 70, 97, 98, 103`—received correction overlays. Original model outputs remain
  immutable and all reviews/corrections are in the audit log.
- Style profile v4 uses 558 training records and a deterministic 140-record holdout.
- The training profile has a median caption length of five words and 26 characters. Questions
  occur in 26.34% of captions, exclamations in 9.86%, and emoji in 0.36%.

Measured profile-v4 results:

| Measure | Result | Interpretation |
| --- | ---: | --- |
| Exact historical image-to-caption recovery | 3/140 (2.14%) | Deterministic projection rarely recovers the one exact caption from visually similar, often ambiguous images. This remains a known failure and must not be represented as solved. |
| Qlob caption ranking | 35/140 (25%) | The channel's real caption ranked above a deterministic contrast caption in 35 holdout cases. |
| Top-three franchise retrieval relevance | 135/140 (96.43%) | At least one same-franchise record appeared in the first three retrieval results for 135 holdout posts. |
| Transformed-duplicate recall | 100% | All controlled transformed duplicates were detected. |
| Unrelated false-positive rate | 0% | The controlled unrelated image was not classified as a duplicate. |
| Reviewed outlier-field accuracy | 4/20 (20%) | This deliberately difficult ten-post uncertainty sample exposed seven records needing overlays. It is not an unbiased catalogue-wide accuracy estimate. |

Because exact caption recovery is poor, LeeWay treats captions as reviewable suggestions. It does
not infer that a generated caption is correct merely because retrieval found visually similar
history.

The local profile and evaluation evidence is written to:

- `data/qlob-production/reports/style-profile-v4.json`
- `data/qlob-production/reports/style-profile-v4.md`
- `data/qlob-production/reports/profile-evaluation.json`
- `data/qlob-production/reports/profile-evaluation.md`

## Live discovery safeguard pass

Discovery was deliberately bounded to two queries, three results per query, and six total
candidates.

- Google presented a challenge page. LeeWay stopped, saved a diagnostic screenshot, and did not
  attempt to bypass it.
- The configured headed Bing Images pass downloaded six candidates with page and direct-image
  provenance.
- Three candidates passed the automated hard filters and remained reviewable.
- One candidate was rejected as a historical duplicate.
- One promotional image was rejected for a prominent watermark.
- One marketplace image was rejected for a prominent watermark and was also flagged with
  near-certain personal-artwork and fan-art warnings.
- Every candidate retained `rights_status=unknown`. A caption does not establish permission to
  reuse an image, so human rights/provenance review remains mandatory.

## Real proposal dry pass

Generation run `2` created proposal `1` for 2026-07-18 at 10:00
`America/Toronto`.

- State: `needs_review`
- Selected candidate: a Homer Simpson reaction still from a Pinterest source page
- Backup candidates: `2` and `3`
- Recommended caption: `Homer is very excited`
- Alternatives: `Homer is ready for something`; `That grin says trouble`
- Caption confidence: 97%
- Grounding references: historical posts `169, 157, 107`
- Factual warning: the specific event or context is not identifiable from the image alone
- Candidate warnings: rights unknown and the relative-date history cannot prove the 180-day
  boundary

The API and production web server were both restarted. Proposal `1` returned unchanged with no
approval timestamp, rejection timestamp, external post ID, or YouTube action.

The dashboard, review, queue, catalogue, profile, settings, and activity pages all rendered
against this real database. The UI showed the original and square preview, caption alternatives,
grounding, source, warnings, historical matches, audit history, and `Publishing disabled`.

## Automated quality gate

- Pytest: 43 passed.
- Ruff: passed.
- Mypy strict mode: 57 source files, no issues.
- ESLint: passed.
- Vitest: two component files and two tests passed.
- Next.js production build: passed; all nine routes compiled and TypeScript passed.
- `leeway doctor`: Python, Node, npm, Playwright Chromium, production data paths, migration
  `0003`, ChatGPT-authenticated Codex, no paid fallback, and the loopback API port all passed.
- Real-database restart check: passed.
- External post/schedule action: intentionally not performed.

## What remains before production completion

The historical database, profile, bounded discovery pass, real proposal, review UI, safeguards,
and restart persistence are validated. The remaining production milestone is a guarded YouTube
publisher plus one explicitly authorized controlled scheduling test:

1. Implement a visible publisher that accepts only a human-approved proposal.
2. Revalidate the selected Qlob delegate session and channel before every submission.
3. Require an explicit final user action before clicking YouTube's Schedule control.
4. Verify the exact image, caption, Toronto date/time, and scheduled-post record on YouTube.
5. Record screenshots and audit evidence and test the Manager-assisted recovery/deletion path.

Until those steps pass, LeeWay is a validated local intelligence and review system, not a complete
autonomous or live scheduling system.
