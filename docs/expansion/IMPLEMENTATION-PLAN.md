# Implementation plan

For the implementing model. Read [`specs/00-principles.md`](specs/00-principles.md) first; its
invariants apply to every work package. Repository rules in `AGENTS.md` still apply (validate,
never touch production data, never commit secrets).

## Order and why

The order is chosen so that each phase is useful to the owner on its own, risk is retired early
by live tests, and nothing hosted is built before the local version proves the design.

```
P0 Foundations ─► P1 Live connection ─► P2 Media + engine ─► P3 Publishers ─► P4 Real-time
                                                                   │
                         P5 Agent interface ◄──────────────────────┤
                         P6 Private instance data (independent; can run any time after P0)
                                                                   ▼
                                                        P7 Tenancy ─► P8 Hosted beta ─► P9 Reviews
```

Tenancy (P7) comes late on purpose. It is the largest migration and is only needed for other
people. Every earlier table is still created with a `workspace_id` column defaulting to 1 (the
column is cheap; isolation enforcement is the expensive part), so P7 does not have to revisit
them.

## Phases

### P0 — Foundations (no user-visible change)

| WP | Work | Spec |
| --- | --- | --- |
| 0.1 | Stop running migrations at import; add `runway db upgrade`; launcher calls it | 01 §6 |
| 0.2 | `RUNWAY_PROFILE` setting with startup validation; `Principal` and `get_principal` on every route, returning the local owner; route-walk test | 01 §2–3 |
| 0.3 | Fix token refresh: transient errors stay transient; per-account lock; lifetimes from responses | 04 §3 |
| 0.4 | Declared state machines for draft, target and comment status; remove free-string writes | 03 §2 |
| 0.5 | `workspaces` table with row 1; `workspace_id` default 1 on platform tables (no enforcement yet) | 01 §4 |
| 0.6 | `publish_jobs` table and `runway worker` process; move the sync scheduler onto it | 03 §3 |

Done when: all existing tests pass; a migration rehearsal on a copy of production shows
identical row counts; the Supervisor health check and `/review` are unchanged.

### P1 — First live connection (owner action required)

Needs the owner to create developer apps (spec 09 stage 1). This phase is mostly testing and
fixing what the documentation-only implementation got wrong.

| WP | Work |
| --- | --- |
| 1.1 | Connect a real YouTube channel; run a full sync; fix every mismatch |
| 1.2 | Connect a real Instagram professional account; same |
| 1.3 | Public edge for callbacks (spec 04 §6); connect a real TikTok account in sandbox; same |
| 1.4 | Record results in `docs/VERIFICATION_REPORT.md` under "Verified live capabilities" |

Live tests: Y-1, Y-2, I-1, T-1 below.

### P2 — Media and the publishing engine

| WP | Work | Spec |
| --- | --- | --- |
| 2.1 | `MediaStore` port, `LocalDiskStore`, chunked upload API, probe job, validation module | 02 |
| 2.2 | Posts and targets model changes, version checks, payload hash, release | 03 §1–2 |
| 2.3 | Attempts ledger, point-of-no-return marking, reconcile jobs, bounded retries | 03 §4 |
| 2.4 | `Publisher` port, `assisted` adapter, mock publishers with failure switches | 03 §5, 08 §6 |
| 2.5 | Post editor, per-target validation, release dialog, calendar integration | 03 §9 |
| 2.6 | "Also post to…" bridge from accepted proposals | 03 §7 |

Done when: the crash-at-every-step test passes against mock publishers; a post completes end to
end through the assisted adapter with no credentials.

### P3 — Real publishers

| WP | Work | Spec | Live test |
| --- | --- | --- | --- |
| 3.1 | YouTube publisher: resumable upload, native scheduling, thumbnail, verify, reconcile | 08 §2 | Y-3, Y-4, Y-5 |
| 3.2 | `R2Store` and signed URLs | 02 §2 | I-2 |
| 3.3 | Instagram publisher: containers, Reels, images, carousels, Stories | 08 §3 | I-3, I-4 |
| 3.4 | TikTok inbox publisher and the mandatory screen component | 08 §4 | T-2 |
| 3.5 | TikTok direct publisher, built and tested against mocks, gated behind `audited` | 08 §4 | later |
| 3.6 | Per-account capability function; regenerate the capability document | 08 §5 | — |
| 3.7 | Vendor adapter, **only if the owner approves a vendor** | 03 §5 | V-1 |

Each first live publish is a supervised acceptance test: an unlisted or private post, announced
to the owner beforehand, with the exact file, caption, time and resulting URL reported.

### P4 — Real-time

| WP | Work | Spec |
| --- | --- | --- |
| 4.1 | `events` table written transactionally; SSE endpoint with resume; web app switches from polling | 04 §4 |
| 4.2 | Adaptive polling and the quota budget; freshness display | 04 §3, §5 |
| 4.3 | Webhook inbox with signature checks; YouTube WebSub subscribe and renew; TikTok events | 04 §2 |
| 4.4 | 30-day refresh-or-delete job for YouTube data | 04 §3 |

### P5 — Agent interface

| WP | Work | Spec |
| --- | --- | --- |
| 5.1 | `/api/v1` conventions, problem+json, idempotency keys, generated TypeScript client | 06 §6 |
| 5.2 | API keys and scopes; approval queue in the UI; `human_required` rules | 06 §3–4 |
| 5.3 | MCP server over stdio | 06 §7 |
| 5.4 | Agent events and outbound webhooks | 06 §8 |

Before 5.2: answer open question Q2 (what Muse calls today).

### P6 — Private instance data

| WP | Work | Spec |
| --- | --- | --- |
| 6.1 | `RUNWAY_INSTANCE_DIR`, settings load order, SOPS decryption in memory | 07 §5 |
| 6.2 | `runway instance` commands with the visibility guard and lease | 07 §4 |
| 6.3 | Secret-scan pre-commit hook and CI job in the public repository | 07 §6 |

Independent of P1–P5. Worth doing **before** P1, so that the first real credentials go straight
into the private instance.

### P7 — Tenancy

| WP | Work | Spec |
| --- | --- | --- |
| 7.1 | Postgres support; CI matrix over both databases | 01 §6 |
| 7.2 | `WorkspaceScoped` mixin, automatic filters, flush guard | 01 §5 |
| 7.3 | Row-level security migration and roles | 01 §5 |
| 7.4 | Isolation test suite for every scoped model and route | 01 §5 |
| 7.5 | Database token store with key versions | 05 §3 |

### P8 — Hosted beta

| WP | Work | Spec |
| --- | --- | --- |
| 8.1 | Identity provider seam and first implementation; workspace, member and role UI | 05 §2 |
| 8.2 | Hosted profile hardening: CORS, CSRF, CSP, rate limits, quotas, kill switches | 05 §6 |
| 8.3 | Direct-to-storage uploads | 02 §4 |
| 8.4 | Disconnect, delete and export flows; platform callbacks; audit log view | 05 §5 |
| 8.5 | Container image, deploy, backups with a rehearsed restore, alerts | 05 §7 |
| 8.6 | Instagram Unified Login adapter | 08 §3 |
| 8.7 | Public website, privacy policy, terms (owner supplies or approves the text) | 09 stage 2 |

### P9 — Reviews

Owner-driven, per spec 09 stage 3. Engineering supports with demo recordings and fixes.

## Live acceptance tests

Each must be run once, by or with the owner, and recorded with date and result. These are the
points where this research could be wrong.

| ID | Test | What it settles |
| --- | --- | --- |
| Y-1 | With the Google project "In production" and unverified: connect, wait 8 days, sync without reconnecting | Whether the 7-day token expiry is avoided |
| Y-2 | Full sync of the real channel; compare ten numbers with YouTube Studio | Parser correctness |
| Y-3 | Upload a short test video as `unlisted` from the new, unaudited project; check it is not forced private | The 2026-11-02 rule change |
| Y-4 | Upload as private with `publishAt` ten minutes ahead; confirm YouTube publishes it | Native scheduling |
| Y-5 | Kill the worker mid-upload; restart; confirm one video results | Resume and reconcile |
| I-1 | Connect and sync the real Instagram account | Token lifetimes, metric names |
| I-2 | Give Instagram an R2 signed URL for an image | Whether Instagram's fetcher accepts it |
| I-3 | Publish one Reel; check `content_publishing_limit` before and after | Flow and the real daily limit |
| I-4 | Publish a Reel file larger than 100 MB | Whether container polling needs longer than 5 minutes |
| T-1 | Connect the owner's TikTok in sandbox; read `expires_in` and `refresh_expires_in` | Token lifetimes |
| T-2 | Inbox upload from the sandbox app | Whether drafts work without an audit |
| V-1 | (If a vendor is approved) one TikTok post through the vendor, confirm it is public | The vendor claim |

If Y-3 fails, YouTube falls back to the same choice as TikTok (audit, vendor, or manual) and
the build-versus-buy recommendation must be revisited. If I-2 fails, serve Instagram media from
a public bucket path with an unguessable name and a short lifecycle instead of a signed URL.

## Testing rules for every work package

- Unit and integration tests use temporary data directories and mock providers only.
- New behaviour gets a test that fails before the change.
- Run `ruff`, `mypy --strict`, the backend suite and `npm run check` before each push; the
  backend suite takes about 40 minutes, so run the affected subset while iterating and the full
  suite before merging.
- Migrations are rehearsed on a copy of the production database with row counts compared.
- No test may publish, reply, or connect to a real platform.

## Rough size

Not a schedule; an ordering of effort for planning. P0 and P2 are the largest engineering
phases. P1 and P9 are dominated by waiting on the owner and on platforms. P3.1 (YouTube) is the
smallest route to something real. P7 is the riskiest migration.
