# Sealed holdout comparison

The selected canonical configuration improved the deterministic no-edit and pairwise
policy proxies from 0/2 to 2/2 on the locked holdout. The observed delta is +100
percentage points.

This is a two-case release check, not a human-preference study. The baseline Wilson
95% interval is `[0.00000, 0.65762]`; the canonical interval is
`[0.34238, 1.00000]`. They overlap, so the result is not statistically conclusive.
It supports the deterministic replacement contract while leaving creator preference
unproven.

| Measure | Frozen baseline | Canonical engine |
| --- | ---: | ---: |
| No-edit acceptance proxy | 0/2 (0%) | 2/2 (100%) |
| Pairwise accuracy proxy | 0/2 (0%) | 2/2 (100%) |
| Grounding pass rate | not directly comparable | 100% |
| Unsupported-claim rate | not directly comparable | 0% |
| Mean local latency | 69.385 ms | 144.071 ms |
| Model calls during scoring | 0 | 0 |

The latency ratio is 2.08x, an absolute increase of 74.686 ms. No latency threshold
was preregistered. The release judgment treats 144.071 ms of local overhead as
operationally immaterial beside model latency, but this does not estimate real Codex
latency.

The locked holdout was opened exactly once. Its sealed outcome is
`HOLDOUT_EVALUATED.json`; no holdout label was used during tuning.
