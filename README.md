# Runway

Runway is a private editorial intelligence system for Qlob's image-based YouTube Community
posts. Its job is not simply to ask an AI for a caption. It collects channel history, understands
images and captions together, finds new candidates, proposes several editorial directions, learns
from every human decision, organizes accepted posts, and prepares them for controlled publishing.

The product is intentionally human-directed. Runway should continuously bring forward strong
options, while the creator remains the final authority over what is accepted and published.

## 1. Backend — ML, Database, and Intelligence Engine

### What the backend is trying to learn

Runway is learning a narrow but difficult preference: **what makes an image-and-caption pairing
feel right for Qlob at this moment?** That is different from learning generic image quality or
generic social-media writing.

A good recommendation depends on several things at once:

- Whether the image is clear, expressive, safe, and relevant to the channel.
- Whether the caption is grounded in what is actually visible.
- Whether the wording matches Qlob's voice and favors useful open-ended questions.
- Whether the combination feels fresh compared with recent options and scheduled posts.
- Whether the creator historically accepts, edits, or rejects this kind of idea.
- Whether the whole group of options is varied, rather than ten individually acceptable versions
  of the same scene.

The backend therefore treats the image, caption, pairing, creator decision, and surrounding
editorial context as separate evidence. A rejection of an image should not automatically teach
the system that every caption attached to it was bad. Likewise, editing a caption is stronger and
more precise evidence than simply accepting or rejecting the complete proposal.

### The end-to-end intelligence flow

The canonical production path works as follows:

1. **Capture and normalize history.** Historical Community posts, captions, media, timestamps,
   and source records are stored locally with stable identities. Capture is resumable and does
   not treat a temporary YouTube loading plateau as the end of the channel history.
2. **Annotate the historical material.** Runway extracts visible characters, entities, actions,
   emotions, setting, composition, scene type, text overlays, and uncertainty. Annotations remain
   versioned so later corrections do not erase what an earlier model believed.
3. **Build representations.** Images, captions, image-caption pairs, and multi-part text evidence
   are converted into comparable numerical representations. These power retrieval and similarity
   without requiring the generator to reread the entire database for every proposal.
4. **Compile a channel profile.** Instead of reducing Qlob to one average style, the current
   profile identifies seven recurring content modes. This lets a reaction post, character debate,
   prediction, comparison, or scene-interpretation post use different evidence and caption
   strategies.
5. **Discover and inspect candidates.** Search providers return possible images. Runway preserves
   source metadata, downloads supported media, rejects unsafe material, detects duplicates, and
   scores editorial potential. NSFW safety is a hard boundary; ordinary source and rights metadata
   remains available for reference rather than acting as the creative ranking objective.
6. **Optimize the complete image slate.** Runway chooses a varied set, not merely the highest
   individual scores. It considers franchise, character, scene, setting, emotion, composition,
   source repetition, visual similarity, recent exposure, and previously scheduled material.
7. **Retrieve bounded evidence.** For each selected candidate, Runway retrieves the most relevant
   historical posts, corrections, preferences, visual examples, and channel rules. Every item used
   in a generation is persisted, which makes the result explainable and reproducible.
8. **Generate a caption slate.** The caption engine proposes a primary caption and alternatives
   using deliberately different editorial angles. Qlob's preferred direction is an open-ended
   question when the image supports one, not a forced question that invents context.
9. **Verify and rerank.** A separate grounding pass checks names, actions, emotions, and claims
   against the image evidence. Preference scoring and diversity logic then choose what should be
   displayed. If nothing survives, the engine abstains instead of fabricating a confident answer.
10. **Learn from review.** Accepts, edits, alternative selections, image replacements, rejections,
    and “fewer like this” signals become normalized evidence for future retrieval and training.

### The production intelligence database

SQLite is the canonical local intelligence store, not a disposable cache. It uses migrations,
foreign keys, write-ahead logging, integrity checks, content hashes, and explicit provenance.

The production snapshot validated on July 22, 2026 contains:

- 771 historical Qlob posts and raw source records.
- 869 media assets and 698 compatible historical annotations.
- 3,612 image, text, and multimodal representation records.
- 33,694 persisted retrieval-evidence records.
- 354 pairwise caption preferences and 90 normalized feedback signals.
- 147 generated caption candidates across 23 caption slates.
- 156 discovered candidate images, 29 proposals, and five versioned style profiles.
- 52 schema-managed tables at migration `0010_neural_intelligence`.

The private GitHub database release is an online SQLite backup rather than a copy of a potentially
inconsistent live file. Its manifest records the source commit, migration, table counts, database
hash, archive hash, integrity result, and foreign-key result. Browser profiles, authentication
state, media caches, raw downloads, logs, WAL files, and temporary backups are excluded.

### Retrieval is the first form of personalization

Runway does not need to change a large model's weights after every click. Most immediate learning
comes from changing the evidence supplied to the next decision.

For example, if the creator repeatedly rejects surprised driving scenes, those decisions can
reduce the relevant scene cluster, character combination, and emotional pattern before the next
caption is generated. If the creator regularly rewrites declarative captions into “why” questions,
the edited captions become high-value positive examples for question structure.

This retrieval-based adaptation is fast, inspectable, and reversible. It also avoids continuously
refitting a model on a tiny and unstable dataset.

### Candidate diversity and anti-repetition

The current slate optimizer is `active-representation-slate-v2`. It combines deterministic
editorial fingerprints with representation similarity and recent exposure history.

Deterministic fingerprints can hard-suppress clear concept repeats, such as another image with the
same franchise, scene family, setting, and emotion. The lightweight local image descriptor is
useful for ranking but is not a trained semantic model, so it applies a soft similarity penalty
rather than blocking an otherwise distinct concept. If a validated neural representation is
activated later, that model may apply hard semantic near-duplicate suppression.

This distinction fixed an important failure mode: visually similar synthetic images previously
caused a request for ten proposals to stop after two, even though the remaining concepts were
editorially distinct.

### The feedback and training flywheel

Runway records more than the final accepted post. It is designed to remember the decision surface:

- Which candidates were eligible and which were actually shown.
- Which primary and alternative captions were displayed.
- Whether the creator accepted, edited, rejected, replaced, or requested fewer similar results.
- The generated caption and the creator's final edited caption.
- Separate image, caption, and image-caption pairing judgments.
- The model, prompt, profile, retrieval run, representation set, and policy involved.

These records support pairwise preference datasets: given two alternatives, which one better
matches the creator's actual behavior? Training is explicit and versioned. Models never silently
refit inside a normal web request.

The lifecycle supports:

- Immutable dataset versions and feature snapshots.
- Time-based training, validation, and holdout splits.
- Active-learning batches that prioritize uncertain or informative choices.
- Offline experiments and comparison metrics.
- Shadow evaluation, where a candidate model scores real work without controlling the UI.
- Blind creator studies that hide which model produced each option.
- Explicit activation, supersession, rollback, and audit history.

### Neural and multimodal model support

The architecture supports trained text and vision-language providers, including local
Sentence-Transformer and SigLIP 2 adapters. Weights must be explicitly supplied from trusted local
files. Runway does not download a model, switch providers, or activate a paid service by itself.

No creator-trained neural preference model is currently active. The live system therefore combines
deterministic representations, structured features, historical retrieval, preference evidence,
and bounded Codex reasoning. This is intentional: activating a complex model before enough genuine
creator labels exist would make the system harder to understand without proving that it is better.

Codex is used as a reasoning component, not as the database. Each call receives a bounded evidence
package and validated image inputs. Calls are ephemeral and serialized. Separately billed API
credentials are removed from the Codex subprocess, and there is no automatic paid-API fallback.
Invalid output, authentication failure, timeout, or included-usage exhaustion stops the current
batch safely.

### How Runway can outperform a general AI model

Runway should not claim that its underlying model is universally more intelligent than a frontier
model. Its advantage is **system-level specialization**.

A powerful general model without the Runway dataset does not automatically know:

- Qlob's complete posting history.
- Which suggestions the creator accepted but edited.
- Which visual concepts have become repetitive this week.
- Which caption structures work for one content mode but fail for another.
- What is already scheduled in the Lineup.
- The creator's separate opinion of the image and the caption.
- The exact examples and corrections that should control today's decision.

Runway can therefore beat a stronger standalone model on the narrow Qlob objective even when the
base model is smaller. It gives the model proprietary memory, a channel-specific objective,
multimodal retrieval, deterministic safeguards, whole-slate optimization, and a feedback loop. A
larger model asked one isolated question has more general capability; Runway has the better brief,
the better evidence, and the ability to learn from the result.

The strongest long-term version is not “Runway versus a frontier model.” It is Runway using the
best appropriate model as one replaceable component inside a system that owns the data, evaluation,
memory, safeguards, and creator relationship.

### Backend aspirations

The next backend milestones are:

- Collect enough real display, accept, edit, and reject events to train a reliable lightweight
  preference reranker.
- Create a larger sealed temporal holdout and define minimum improvements for acceptance rate,
  caption edit distance, factual accuracy, and batch diversity.
- Run the first genuine shadow comparison and blind creator study.
- Evaluate a trusted local neural image representation against the deterministic baseline.
- Activate trained components only when they beat the current engine without reducing grounding or
  diversity, with one-step rollback available.
- Use active learning to request the most informative decisions instead of collecting random labels.
- Eventually train separate image preference, caption preference, pairing, and content-mode models
  rather than one opaque score that tries to represent everything.

## 2. Frontend — Generator, Lineup, and Product Experience

### Current product structure

The frontend is a responsive Next.js and TypeScript application organized around two primary work
areas: the **Generator** and the **Lineup**. Supporting tools remain available without competing
with the daily review loop.

### Generator

The Generator is the continuous editorial desk:

**Discover image → Generate captions → Review → Reject, Edit, or Accept → Show the next option**

It currently provides:

- A large image preview with a square feed preview.
- One primary generated caption and structural alternatives.
- Direct caption editing, including normal punctuation such as `?` and `!`.
- Reject, Edit, and Accept as the dominant actions.
- Caption regeneration and image replacement.
- Persisted progress when navigating away and returning.
- Disabled/loading states that prevent duplicate submissions.
- Error handling that preserves the creator's unsaved caption.
- Immediate transition to the next proposal after a successful decision.

Accepting a proposal is deliberately local. It places the proposal into the next available Lineup
slot; it does not open YouTube, create a browser queue, or claim that anything was published.

### Lineup scheduler

The Lineup is the operational calendar for accepted posts. It supports:

- Calendar and upcoming-post views.
- Image, caption, status, date, time, and timezone previews.
- One Runway-managed post per local calendar date.
- Exact date and time editing with daylight-saving-aware validation.
- Moving a post earlier or later.
- Swapping two posts when the destination date is occupied.
- Editing a scheduled caption before external publishing begins.
- Removing eligible local posts with explicit confirmation.
- Clear published, failed, warning/unverified, and local-only status labels.
- Protection against changing published, publishing, or unverified posts.
- An assisted publishing workspace with prepared images, captions, timestamps, downloads, and copy
  controls.
- A separately authorized serial browser-publishing outbox behind multiple explicit interlocks.

Local Lineup edits never pretend to update YouTube. External actions happen only through an
explicit confirmed Lineup action. If Runway cannot determine whether YouTube accepted the final
Schedule click, the post becomes `publish_unverified`; it is paused for verification instead of
being submitted again and risking a duplicate.

### Supporting sections

- **Archive:** searchable historical and completed proposal records, including the context needed
  to understand past decisions.
- **Activity:** an immutable timeline of important generation, review, scheduling, synchronization,
  and failure events.
- **Profile:** readable diagnostics for channel intelligence, content modes, feedback readiness,
  representation status, and active model state.
- **Settings:** validated scheduling, runtime, and safety preferences with real loading, success,
  and failure behavior.
- **Platform Connection:** a step-by-step explanation of channel permissions, publisher login,
  observed capabilities, connection state, and safe browser setup. It does not show fake Connect
  buttons for operations Runway cannot perform.

### Frontend reliability already implemented

The production UI pass includes focused protections for:

- Double-clicks and duplicate requests.
- Network failures and malformed API responses.
- Success messages only after confirmed success.
- Past-date and timezone-aware scheduling validation.
- Keyboard navigation, visible focus states, dialog focus management, and reduced motion.
- Desktop, tablet, and 390-pixel mobile layouts without horizontal overflow.
- Status communication that does not rely on color alone.
- Confirmation before destructive actions.
- Immutable controls for externally sensitive post states.

The current automated frontend gate covers 63 tests, ESLint, TypeScript, and a complete Next.js
production build.

### Frontend issues and aspirations

The interface is functionally complete for local review and Lineup management, but several areas
still need real-world refinement:

- Image discovery can exhaust or be blocked by an upstream provider. The UI needs better recovery,
  source switching, and explanation when no safe distinct candidate can be produced.
- Connection status is based on observed browser capabilities. It cannot cryptographically prove
  that the signed-in Google account has a particular YouTube Studio role.
- Browser authentication and YouTube DOM behavior can change independently of Runway.
- Authorized publishing needs a controlled creator-supervised acceptance pass against the real
  channel before it can be treated as proven production automation.
- Large Lineups need more real-use testing for rapid swaps, repeated movement, filtering, and
  long-session performance.
- The activity and profile pages can become more actionable by linking warnings directly to the
  affected post, candidate, experiment, or recovery action.
- Model comparisons, blind studies, and active-learning decisions need dedicated creator-friendly
  screens rather than relying mainly on backend commands and reports.
- The product should eventually explain recommendations in plain language, such as “shown because
  this scene type has performed well, but the character has not appeared recently.”

The design goal remains a fast editorial runway: open the app, see a strong option, make one clear
decision, and immediately receive the next one without administrative clutter.

## 3. Final Issues — Work Remaining Before the Full Vision Is Complete

### Highest-priority production issues

1. **Complete a controlled live publishing acceptance test.** The scheduler and browser publisher
   are extensively tested offline, but a real final Schedule click has not been included in
   automated QA. The first pass should use one disposable or intentionally scheduled post with the
   creator watching every step.
2. **Prove recovery from ambiguous YouTube outcomes.** Runway must demonstrate that an interrupted
   or unclear final click enters `publish_unverified`, can be verified later, and never creates a
   duplicate post.
3. **Harden discovery continuity.** “Generate more” should automatically search additional bounded
   providers or explain the precise blocker. Runway cannot guarantee that every external search
   engine will return usable images, but the product should avoid silent dead ends.
4. **Improve connector confidence.** Record the exact channel page observed, last successful
   capability check, authenticated browser profile, and last verification time. Continue describing
   the role as observed capability rather than claiming an Editor role that cannot be proven.

### ML and database work still required

1. **Collect genuine exposure data.** The production database currently contains historical
   feedback, but the new candidate-exposure system has not yet accumulated a meaningful live sample.
   Training without knowing what was shown would bias the model toward selected items only.
2. **Increase creator labels.** The present pairwise evidence is enough to exercise the lifecycle,
   not enough to justify a powerful personalized neural model. The first useful target is roughly
   500 clean creator decisions; 1,000 or more would provide a stronger training base.
3. **Train the first lightweight reranker.** Start with existing representations and structured
   features. This is cheaper, easier to evaluate, and more data-efficient than fine-tuning a large
   caption model immediately.
4. **Run shadow and blind evaluations.** No candidate model should control production until it
   beats the current engine on a sealed holdout and in a creator-blind comparison.
5. **Evaluate neural representations.** Optional trained adapters exist, but their dependencies and
   weights are not installed or authorized. A candidate must demonstrate better semantic diversity
   and retrieval without increasing false suppression.
6. **Resolve preserved historical warnings.** Three invalid vectors remain in inactive representation
   sets, and 21 older displayed caption candidates predate complete exposure logging. They do not
   affect the active engine, but should remain explicitly excluded or repaired before broader
   historical training.
7. **Expand evaluation depth.** The current test suite proves correctness and safety; it does not
   prove that creator preference quality has reached its ceiling. Future reports need confidence
   intervals, mode-by-mode results, diversity metrics, factual-error rates, and longitudinal drift.
8. **Rehearse backup restoration.** Snapshot creation, upload, hashing, integrity, and foreign-key
   checks are verified. A documented restoration rehearsal should periodically prove that the
   private release can rebuild a clean environment.

### Frontend and workflow work still required

1. Add real-browser end-to-end coverage for long review sessions, rapid repeated actions, Lineup
   swaps, network interruption, and browser refresh recovery.
2. Improve recovery controls for failed image discovery, failed caption generation, and failed or
   unverified YouTube actions.
3. Add creator-facing experiment, blind-study, active-learning, and model-activation screens.
4. Continue testing calendar usability with dozens of accepted posts and across daylight-saving
   boundaries.
5. Add better archive filtering for accepted, edited, rejected-image, rejected-caption, published,
   failed, and model-version history.
6. Make explanations and warnings actionable without turning the main Generator into a technical
   dashboard.

### Platform constraints that Runway cannot solve alone

- The official YouTube Data API does not expose Community-post creation or scheduling. Reliable
  API-based Community publishing is therefore unavailable unless YouTube adds that capability.
- YouTube's web interface and authentication defenses may change. Runway must pause for manual
  login, account selection, CAPTCHA, consent, or an unfamiliar layout rather than bypassing those
  protections.
- External image-search providers can throttle, challenge, remove, or alter results. Runway can
  use multiple providers and preserve provenance, but cannot guarantee unlimited third-party
  availability.
- A general AI model can assist with reasoning, but it cannot substitute for genuine creator
  decisions. Synthetic preferences are useful for engineering tests and must not be presented as
  evidence that the personalized model has learned the creator.

### Longer-term product aspirations

- A genuinely personalized multimodal preference model trained on Qlob decisions.
- Adaptive content-mode rotation that balances proven formats with measured exploration.
- Automatic active-learning sessions that surface the decisions most valuable to future quality.
- Multi-channel intelligence with strict separation between each channel's data and preferences.
- A hosted account and onboarding layer if Runway moves beyond its current private local product.
- A versioned provider marketplace for search, analysis, generation, and publishing boundaries.
- Platform-native Community publishing if YouTube eventually provides an official API.

### Definition of the full vision being complete

Runway can be considered fully complete when:

- The creator can repeatedly review and schedule proposals without silent generation dead ends.
- Every displayed option and creator decision is captured as trustworthy learning evidence.
- A trained model has beaten the deterministic baseline in shadow and blind evaluation.
- The Lineup safely enforces one Runway post per day in the configured timezone.
- Assisted publishing is reliable, and authorized publishing has passed a supervised real-channel
  acceptance and recovery test.
- Database backup and restoration are both rehearsed and verified.
- Frontend, backend, migration, accessibility, and browser end-to-end checks are green on the exact
  production commit.
- The remaining limitations are external platform constraints rather than unresolved Runway logic.
