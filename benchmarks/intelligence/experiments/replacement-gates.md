# Canonical replacement gates

Decision: **canonical replacement approved** for the deterministic, local Runway
intelligence contract.

This decision does not claim human-preference superiority. The sealed two-case
holdout improved the deterministic no-edit and pairwise policy proxies from 0/2
to 2/2, but the Wilson intervals overlap. A blind creator study remains required
before making a human-quality claim.

## Hard gates

| Gate | Result | Primary evidence |
| --- | --- | --- |
| Zero channel-data leakage | Pass | Channel-isolation and five-channel same-image tests |
| Zero migration corruption | Pass | Upgrade/downgrade test plus verified production migration artifact |
| Zero publishing-safety regression | Pass | Feature-gate, confirmation, resume, and idempotency tests |
| Zero hidden paid-provider fallback | Pass | Provider-registry and Codex-auth guard tests |
| Zero rights-policy regression | Pass | Explicit policy precedence and unknown-rights blocking tests |
| Zero duplicate-safety regression | Pass | Exact, transformed, unrelated, and source-URL tests |
| Zero unsupported critical entity claims | Pass | Wrong entity, event, quote, relationship, and confidence tests |
| Required deterministic workflows | Pass | Fusion, split, and end-to-end workflow tests |
| Required rollback procedures | Pass | Populated 0006 upgrade/downgrade test and production backup |

## Critical regression cases

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

## Release checks

| Check | Result |
| --- | --- |
| Critical JUnit suite | 51 passed, 0 failed, 0 skipped |
| Cross-channel generalization | 5/5 fixtures passed |
| Locked holdout opened exactly once | Pass |
| One canonical runtime engine | Pass |
| Secondary metrics within release bounds | Pass |
| Paid model calls | 0 |
| Live publication actions | 0 |

Local deterministic holdout latency increased from 69.385 ms to 144.071 ms
(+74.686 ms, 2.08x). This is a real regression in relative terms. No latency
threshold was preregistered; the release judgment treats the absolute overhead
as operationally immaterial beside the model-backed workflow. Real Codex latency
remains unmeasured.

See `replacement-gates.json` for each test identifier, metric, and artifact path.
