# Current limitations and external blockers

This document separates unfinished Runway work from constraints imposed by YouTube, Google,
account state, or missing creator evidence. It is intentionally conservative: an unknown or
unverified capability is not presented as working.

## Community-post publishing boundary

YouTube's Data API does not expose a Community-post resource, and invited channel users cannot
operate YouTube APIs for the delegated channel. A normal OAuth integration therefore cannot replace
native posting or make browser automation an official API.

What works now:

- acceptance and normal Lineup editing remain local and create no external work;
- default assisted mode validates and orders exact native posting instructions without a browser;
- an explicitly authorized Lineup action can enter the persisted, serialized browser outbox;
- the authorized worker processes one post at a time;
- session, channel identity, composer access, submission, and scheduled-post presence are checked;
- ambiguous submissions become verification-only and are never automatically retried;
- the last connection check is persisted and can be read without opening Chrome.

The current restart-safe coordinator still opens separate bounded browser contexts for an item's
worker preflight and scheduling action. The explicit batch route avoids another immediate live
validation launch, but safe context reuse would require a coordinator/adapter session contract and
remains future work.

What remains:

- obtain platform authorization and legal/security review before any broader browser deployment;
- any future headed remote worker remains browser automation, not an official API;
- provide a secure one-time interactive login surface for that worker;
- add worker heartbeat, restart supervision, encrypted session storage, and per-customer isolation.

Runway will not add stealth automation, CAPTCHA bypass, or automatic Google-challenge handling.

Official references:

- <https://developers.google.com/youtube/v3/getting-started>
- <https://support.google.com/youtube/answer/9481328>

## What the connector can prove

Runway can prove operational capabilities by checking the configured channel, active Qlob identity,
Community posting controls, and composer. It cannot reliably query the exact delegated role through
an API. The UI therefore says that capabilities were verified; it does not falsely claim that
YouTube returned an `Editor` role.

The passive status is last-known state:

- `Connected` means the latest browser capability check passed;
- `Check is stale` means a successful check is more than 24 hours old;
- `Needs attention` means the latest check failed;
- `Unchecked` means no durable result exists;
- `Disabled` means authorized browser work is off; assisted mode remains usable.

Only a fresh browser check can prove the current Google session. Reading the saved status never
opens Chrome.

## Editor permissions

`Editor (Limited)` is the least-privilege recommendation because it can create posts without
revenue access. YouTube documents Community-post deletion as Manager-only. Runway therefore treats
published posts as immutable.

Runway treats externally confirmed posts as immutable in normal Lineup editing and removal. Any
future explicit external mutation flow must separately confirm the exact action and observed native
capability, then preserve uncertain-outcome safeguards. Automated tests use a non-network adapter
and never mutate Qlob.

Assisted completion checkboxes are intentionally session-local. Durable manual progress would
require a persistence/schema decision that is outside this refactor.

## Intelligence and training

The historical corpus, retrieval representations, diversity fingerprints, feedback normalization,
pairwise-preference pipeline, datasets, evaluation gates, and activation controls are implemented.
That does not mean a learned preference model is active.

The Profile page reports the current truth:

- normalized caption, image, and pairing signals;
- human pairwise labels;
- preference datasets;
- active model targets;
- creator blind-study progress.

Generator decisions improve retrieval immediately. Model weights activate only after enough genuine
creator labels exist and the held-out quality, calibration, integrity, and isolation gates pass.
Synthetic shadow-editor decisions are never treated as creator ground truth.

Readiness is calculated separately for caption, image, and pairing targets. Eight labels spread
across several targets do not make any one target trainable, and held-out/evaluation labels do not
count as training evidence. Profile shows the training-eligible count and threshold for each target.

The highest-value missing evidence is creator judgment, not another unrestricted archive crawl:

1. use `Fewer like this` for repetitive visual clusters;
2. edit captions into the preferred wording;
3. accept and reject normally;
4. complete the 50-case blind creator study;
5. train and evaluate caption, image, and pairing targets separately.

## Audience performance

Runway does not yet import Community-post impressions, likes, comments, or normalized engagement.
Audience performance must remain separate from creator preference and must be normalized for post
age and exposure before it influences ranking.

This cannot be considered complete until a documented, permitted source of Community-post analytics
is available and its data quality is validated.

## Creator platform connections

The platform layer (Accounts, Analytics, Inbox, Content, Drafts) is implemented against the
official YouTube, Instagram, and TikTok APIs with offline tests on recorded response shapes. The
exact per-capability status is in [`PLATFORM_CAPABILITIES.md`](PLATFORM_CAPABILITIES.md). The
honest summary:

- **No live account has been connected during development.** The OAuth flows, sync steps, and
  parsers are verified against documented payloads and mocked transports, not against a real
  channel or profile. Expect first-run surprises per platform (field renames, scope prompts) and
  treat the first live connection as an acceptance test.
- **Publishing videos, Reels, and TikToks is not implemented.** YouTube `videos.insert` is
  possible but not built; Instagram publishing needs media on a public URL that loopback-only
  Runway cannot provide; TikTok Direct Post needs an app audit and otherwise posts privately.
  Drafts record manual publication and link to the imported post after the next sync.
- **Comments on TikTok are read-only elsewhere.** TikTok's Display API has no comments; the
  Business Account API is a separate product and is not integrated.
- **Replies are off by default** and, when enabled, are limited to YouTube (with the
  `force-ssl` scope) and Instagram. Another system (the private Qlob Manager) already replies on
  the Qlob accounts; running both would create two responders.
- **Analytics are per platform and delayed.** YouTube Analytics finalizes with a delay of about
  two days; TikTok has no daily series at all, so TikTok trends come only from Runway's own sync
  snapshots. Nothing is blended across platforms, by design and by YouTube policy.
- **TikTok local OAuth needs an HTTPS tunnel** because TikTok rejects loopback redirect URIs.
- **Sync is polled, not real time.** Webhooks (Instagram comment webhooks, YouTube push
  notifications) are not implemented. Data freshness is shown everywhere; manual "Sync now" and
  an optional interval scheduler are the only triggers.
- **Single workspace.** Multiple accounts per platform are supported by the schema and UI, but
  there is still one local user and one token store.

## Signup and multiple customers

Runway remains a local, single-user product pinned to Qlob. It does not currently provide:

- hosted signup or account recovery;
- workspaces or application roles;
- tenant-isolated databases and media;
- a dedicated publisher identity per customer;
- remote session custody, billing, privacy export, or deletion workflows.

A `+alias` is not an isolated Google identity. A hosted version should provision or require one real
publisher identity and one isolated worker per customer/workspace. Building a signup page before
those boundaries exist would create a misleading and unsafe product surface.

## Test boundary

Routine automated and browser tests must use temporary data, mock model providers, and a fake
YouTube adapter. They must not consume ChatGPT usage, submit a Community post, edit Qlob, remove a
scheduled post, or click YouTube's final Schedule control.

A live acceptance test requires an explicit production instruction and must report the exact image,
caption, date, time, external URL, and verification result.

## Roadmap

These are planned, not implemented:

- Collect enough genuine display, accept, edit, and reject events to train a lightweight
  preference reranker. The product gate is 100 clean comparisons per target plus a sealed blind
  evaluation.
- Build a larger sealed temporal holdout with minimum improvements for acceptance rate, caption
  edit distance, factual accuracy, and batch diversity.
- Run the first genuine shadow comparison and blind creator study.
- Evaluate a trusted local neural image representation against the deterministic baseline, and
  activate trained components only when they beat the current engine without reducing grounding
  or diversity.
- Complete one supervised live publishing acceptance in authorized browser mode.
- Add UI for experiments, blind studies, and model activation if those workflows become frequent,
  and richer Archive filters.
