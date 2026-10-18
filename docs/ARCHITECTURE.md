# Architecture

Runway has one canonical production intelligence path. The CLI, FastAPI routes,
and Next.js UI call service-layer operations; services own capture, catalogue,
analysis, retrieval, captions, generation, proposals, feedback, scheduling, and
audit events. SQLAlchemy repositories and lifecycle services own SQLite
transactions. Typed provider/runtime protocols isolate external integrations.

## Canonical intelligence flow

```text
channel history + media + corrections + policy + recent state
                              |
                              v
                 canonical channel evidence
                              |
                              v
        explicit active text/image/multimodal sets
                              |
                              v
      persisted hybrid + reference-image/text retrieval
                              |
                              v
                    typed editorial brief
                              |
                              v
        caption/image candidates through safe providers
                              |
                              v
      grounding + policy + rights + duplicate verification
                              |
                              v
       separate image/caption/pairing ranking and slate
                              |
                              v
             creator select/edit/accept/reject
                              |
                              v
      append-only event + normalized target feedback
                              |
                              v
 immutable feature snapshots + pairwise preferences
                              |
                              v
 frozen dataset -> explicit training -> gated activation
                              |
                              +---------- feeds future retrieval/ranking
```

The database is the durable evidence, lineage, training, evaluation, activation,
and rollback layer. It is not a model-weight fine-tuning store. Production
scoring loads a persisted active model when one is qualified; otherwise the
canonical deterministic fallback remains explicit and no request-time fitting
occurs.

## Representation resolution

`representation_sets` freeze the exact expected channel/entity/content/provider
plan. Bounded set items checkpoint backfill work and link to exact
content/configuration-addressed `representation_records`. Validation checks
coverage, identity, files, dimensions, finite values, normalization, and
contract consistency before activation.

Retrieval resolves one explicit active set per `(channel, scope, purpose)`. It
never chooses a row because it is newest. Retrieval runs persist active set IDs,
provider/model/configuration identity, considered and selected evidence, and
cache hit/miss/recomputation/stale diagnostics.

## Typed intelligence harness

The harness persists a parent run and bounded attempts with typed inputs and
outputs, provider/model/prompt identity, budgets, timeouts, usage, artifacts,
errors, and abstention. Missing capabilities fail without fallback. Its registry
contains no publishing or platform-mutation capability.

Optional neural representation adapters are lazy and local-files-only. They do
not download weights, trust remote code, activate themselves, or replace the
deterministic baseline without frozen evaluation and an explicit transactional
activation.

## Decision and learning path

Proposal events are append-only decision authority. Idempotent derivation links
each learning row to its source event. Human caption edits become first-class
verified candidates with parent lineage, immutable feature snapshots, and an
exact text representation.

Caption, image, and pairing labels remain separate. Deterministic,
group-protected datasets freeze decision-time features. Explicit training jobs
persist model parameters, metrics, calibration truth, and artifact hashes.
Activation and rollback append auditable records and preserve superseded
artifacts.

## External platform boundary

The YouTube publisher is a separate service boundary and cannot be invoked by
the intelligence harness. Generator acceptance records the existing feedback
and reserves an editable local Lineup slot only. Normal Lineup edits invalidate
stale payloads locally without queueing or opening a browser.

Only a deliberate, confirmed Lineup action crosses the external boundary.
Default `assisted` mode returns validated posting instructions without browser
work. Separately gated `authorized_browser` mode enters the existing restart-safe
serial FIFO outbox. Queue creation is not external success; every item is
verified independently. The current one-Runway-post-per-local-day policy remains
enforced while each post may use its own timezone-aware time.

The API listens only on `127.0.0.1` and serves media from the configured local
data directory. The offline fixture path uses the mock runtime. The real caption
runtime uses the project-local Codex CLI with ChatGPT authentication and no paid
API fallback.

## Creator platforms

The creator-platform layer sits beside the editorial engine and never calls into it. It adds
connected accounts, synchronization, a content library, an inbox, analytics, and
cross-platform drafts for YouTube, Instagram, and TikTok. The research behind it is in
[`PLATFORM_RESEARCH.md`](PLATFORM_RESEARCH.md); what each platform can actually do is in
[`PLATFORM_CAPABILITIES.md`](PLATFORM_CAPABILITIES.md).

```text
Next.js pages (/dashboard /accounts /analytics /inbox /content /drafts)
        |
        v
FastAPI platform routes (/api/platforms, /api/overview, /api/content, /api/analytics,
                         /api/inbox, /api/drafts)
        |
        v
runway.platforms services
  |- registry        live or mock providers, redirect URIs, token store
  |- oauth           single-use state rows (10 minute TTL), PKCE for Google
  |- sync            bounded per-account runs: identity -> content -> content metrics
  |                  -> daily metrics -> comments; cursors; step-level errors
  |- content/inbox/drafts/overview   read models and the few local writes
  `- providers       youtube | instagram | tiktok | mock, one capability matrix each
        |
        v
SQLite tables (connected_accounts, platform_content, comment_records, account_metric_days,
               account_metric_snapshots, content_metric_snapshots, sync_runs, sync_cursors,
               platform_actions, reply_drafts, content_drafts, draft_targets, content_groups,
               oauth_states)  +  data/secrets/ (Fernet-encrypted tokens, key file)
```

Design rules that the code enforces:

- **Capabilities are data.** Each provider returns a matrix of
  `implemented | supported_not_implemented | approval_required | unsupported | unverified`
  entries with required scopes. A capability is *ready* for an account only when it is
  implemented, the account is connected, the scopes were granted, and any interlock setting is
  on. The UI reads `ready` and never enables an action that is not.
- **No blended metrics.** Every number carries its platform, metric, source, and collection
  time. Dashboard and analytics never sum YouTube, Instagram, and TikTok values together.
- **Reads are bounded and observable.** A sync run records each step's status, item count, and
  estimated quota. A failing step leaves the run `partial`; an expired token marks the account
  `needs_reauth` and stops that account only. Nothing in sync can take the web app down; pages
  show the last data with its timestamp.
- **Writes are explicit.** The only external mutation is a comment reply. It requires
  `RUNWAY_INBOX_REPLIES_ENABLED=true`, the engage scope on that account, a per-reply
  confirmation, and a ledger row in `platform_actions` that moves
  `proposed -> attempted -> completed -> verified`; an ambiguous outcome becomes `uncertain`
  and is never retried automatically. Video, Reel, and TikTok publishing is not implemented;
  drafts record manual publication instead.
- **Tokens never enter the database or logs.** They live in `data/secrets/platform-tokens.json`
  encrypted with a key in `data/secrets/token.key`; the database stores only an opaque
  `token_ref`. Persisted request/response JSON is redacted of token-like fields.
- **Mock providers are a mode, not a fallback.** `RUNWAY_PLATFORM_PROVIDER=mock` serves
  fictional accounts and shows a persistent banner; `live` (default) never falls back to mock
  data when a provider fails.

The Community-post path is untouched: Lineup, the assisted workspace, the authorized browser
publisher, and the connector keep their own routes, services, and interlocks.

Detailed references:

- [`INTELLIGENCE_DATA_MODEL.md`](INTELLIGENCE_DATA_MODEL.md)
- [`INTELLIGENCE_OPERATIONS.md`](INTELLIGENCE_OPERATIONS.md)
- [`INTELLIGENCE_PROVIDER_MATRIX.md`](INTELLIGENCE_PROVIDER_MATRIX.md)
- [`BLIND_CREATOR_STUDY.md`](BLIND_CREATOR_STUDY.md)

## Editorial pipeline, stage by stage

1. **Capture.** Historical Community posts, captions, media, and timestamps are stored with
   stable identities. Capture is resumable and does not treat a temporary loading plateau as the
   end of the history.
2. **Annotation.** Each post gets versioned annotations: characters, entities, actions, emotion,
   setting, composition, scene type, text overlays, and uncertainty. Corrections are overlays,
   so earlier model output is never erased.
3. **Representations.** Images, captions, image-caption pairs, and text evidence become
   comparable vectors for retrieval and similarity.
4. **Channel profile.** The profile describes several content modes (reaction, character debate,
   prediction, comparison, scene interpretation, and so on) instead of one average style.
5. **Discovery.** Search providers return candidate images. Runway keeps provenance, downloads
   through the bounded downloader, rejects unsafe or duplicate material, and scores editorial
   potential.
6. **Slate optimization** (`active-representation-slate-v2`). The chosen set is varied, not just
   the top individual scores. Deterministic editorial fingerprints can hard-suppress clear concept
   repeats; the lightweight local image descriptor only applies a soft similarity penalty because
   it is not a trained semantic model.
7. **Evidence retrieval.** Relevant historical posts, corrections, preferences, visual examples,
   and channel rules are retrieved and persisted for every generation.
8. **Visual consensus.** The stored annotation is treated as a hypothesis. A blind multimodal
   audit examines the full frame and nine overlapping detail crops; ambiguous or risky actions get
   a second independent audit. Only independently agreed facts are kept, and disagreements sit
   behind an uncertainty firewall. Exact-frame source metadata may corroborate a fact; a search
   query never can.
9. **Caption slate.** A primary caption and alternatives are drafted from the evidence and the
   reconciled facts, using deliberately different editorial angles. Open-ended questions are
   preferred when the image supports their premise.
10. **Grounding audit and rerank.** A fresh multimodal judge decomposes each caption into factual
    premises and must score it at least 0.85. A question may leave its answer open, but its
    premise must be visible. If nothing survives, the engine abstains.
11. **Learning from review.** Accepts, edits, alternative selections, image replacements,
    rejections, and "fewer like this" signals become normalized, append-only evidence for future
    retrieval and training. Image, caption, and pairing judgments are stored separately, so
    rejecting an image does not teach the system that its caption was bad.

Most personalization happens through retrieval: changing the evidence supplied to the next
decision is fast, inspectable, and reversible, and it avoids refitting a model on a small,
unstable dataset. Training is explicit and versioned (frozen datasets, time-based splits, shadow
evaluation, blind studies, gated activation with rollback); nothing refits inside a web request.
