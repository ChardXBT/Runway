# LeeWay completion guide

LeeWay is complete as a local, real-data-validated intelligence and review application. It is not
yet complete as a production Qlob posting system because the live YouTube publisher and one
controlled external scheduling test have not been implemented and proven.

## 1. Prove delegate access before building the Qlob database — verified

Verified on 2026-07-17 without submitting a post:

- YouTube identified the selected Qlob channel session as `You're an editor`.
- Qlob's Create menu exposed `Create post`.
- The composer accepted caption text and exposed the image attachment control.
- Entering harmless preflight text enabled the Post and scheduling controls.
- `Schedule post` opened date, time, and local-timezone controls.
- The final Post and Schedule actions were not clicked, the preflight text was discarded, and no
  LeeWay database or historical-capture operation ran.

What the user provides:

- A Google account invited to Qlob as Editor (Limited), or Editor if already configured.
- Manual sign-in and any Google consent, account-selection, or security challenge.

What to do:

1. Open YouTube Studio while signed into the delegate Google account.
2. Switch to Qlob.
3. Open the Community/Posts composer.
4. Confirm `Create post` is visible.
5. Create a harmless draft, open the schedule menu, and confirm date, time, and timezone controls
   are available. Cancel without publishing.

Pass condition: the delegate can open both the composer and scheduling controls. Keep a Manager
available for recovery because an Editor cannot delete a published Community post.

## 2. Verify the included-usage model runtime

What the user provides:

- A paid ChatGPT account with Codex usage.
- Browser sign-in if the project-local Codex CLI is not already authenticated.

What LeeWay needs:

```dotenv
LEWAY_AGENT_RUNTIME=codex
LEWAY_CODEX_MODEL=gpt-5.6-luna
LEWAY_CODEX_REASONING_EFFORT=low
OPENAI_API_KEY=
OPENAI_MODEL=
```

Run:

```powershell
.\.venv\Scripts\leeway.exe agent login
.\.venv\Scripts\leeway.exe agent status
.\.venv\Scripts\leeway.exe agent smoke
```

Pass condition: the status says ChatGPT authentication, Luna, low reasoning, serialized jobs, and
no paid API fallback, and the temporary schema-and-image smoke request passes. If included usage is
exhausted, LeeWay must stop instead of selecting another provider.

## 3. Build the real Qlob database — verified

What the user provides:

- The same delegate Google account signed into LeeWay's dedicated visible browser profile.
- Manual handling of consent or challenges.
- Time to leave the browser open during the resumable read-only capture.

Run:

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCQ-nHijGwxNU3Go_wyLQ5Ng/posts" --headed --resume
.\.venv\Scripts\leeway.exe catalog verify
```

Verified on 2026-07-17: capture reached a repeatedly stable bottom at 771 unique post IDs and
produced 771 normalized posts, 771 raw records, 771 immutable DOM snapshots, 732 post-to-media
links, and 716 unique historical media files. Of those posts, 698 are image+caption records
eligible for visual annotation. Source URLs were retained and verification found no missing
caption/image, broken file, duplicate external ID, unmatched raw record, capture error, or
diagnostic. A full dry reparse scanned and matched all 771 snapshots with zero errors.

## 4. Build and evaluate Qlob style retrieval — verified with a documented limitation

What LeeWay uses:

- Historical images and captions from SQLite/local media.
- Structured Codex annotations of the actual images.
- Deterministic caption statistics, visual descriptors, similarity edges, and user corrections.

Run:

```powershell
.\.venv\Scripts\leeway.exe analyze history --resume
.\.venv\Scripts\leeway.exe profile build
.\.venv\Scripts\leeway.exe profile evaluate
```

Review inaccurate annotations in the UI, add corrections, and rebuild. This is the project's
"tuning" step: retrieval and profile adaptation, not model-weight training.

Verified on 2026-07-17: all 698 image+caption records have
`historical-annotation-v2` annotations and all 243,253 pairwise similarity edges. Profile v4 uses
558 training and 140 deterministic holdout records. Ten uncertainty/outlier posts remain visually
reviewed and seven retain non-destructive correction overlays. Top-three franchise retrieval
measured 135/140 (96.43%), transformed-duplicate recall 100%, and unrelated false positives 0%.

Exact historical image-to-caption recovery measured 3/140 (2.14%), while Qlob-caption
vs deterministic-contrast ranking measured 35/140 (25%). LeeWay therefore does not treat caption
generation as an exact-match task; every suggested caption still requires human review.

## 5. Prove image discovery and safeguards — bounded pass verified

What the user provides:

- Manual challenge handling if the headed search surface asks for it.
- Human decisions about provenance and image reuse.

What LeeWay does:

- Creates search plans from underused Qlob topics.
- Excludes fan art, personal artwork, portfolios, commissions, and independent illustrations.
- Downloads actual image files, preserves page/image URLs, checks duplicates and quality, runs
  image analysis, and ranks candidates.
- Rejects likely unsafe, watermarked, fan-art, and personal-artwork candidates.

Run:

```powershell
.\.venv\Scripts\leeway.exe discover images --days 10 --provider browser --live
```

Verified on 2026-07-17 with two queries and six total results. Three candidates remained
reviewable; a duplicate and two prominently watermarked images were rejected. One rejected image
also triggered personal-artwork and fan-art warnings. A Google challenge caused a safe stop with
no bypass; the configured Bing pass then completed. All candidate rights remain `unknown`.

## 6. Prove captions and review UI — real read-only pass verified

What LeeWay uses:

- The selected image itself.
- The active Qlob style profile.
- Relevant visual/caption examples, negative feedback, recent exclusions, and rotation state from
  the database.

Run:

```powershell
.\.venv\Scripts\leeway.exe generate batch --days 10
.\.venv\Scripts\leeway.exe queue status
.\run-leeway.ps1
```

In the UI, test candidate replacement, all three caption options, editing, rejection, regeneration,
approval, rescheduling, duplicate warnings, and persistence after restart.

Verified on 2026-07-17 with one real proposal. The UI rendered the original and square preview,
three caption choices, grounding rationale/confidence/reference posts, provenance, rights and date
warnings, closest history, queue status, settings safeguards, and audit events. The proposal
survived API and web restarts as `needs_review`. Fixture tests cover edits, rejection, replacement,
approval, internal scheduling, and persistence. No real approve, schedule, or posting control was
submitted during this validation.

## 7. Implement the real YouTube scheduler

What the user provides:

- Authorization to use the Qlob delegate session for a controlled external-post test.
- A test caption/image/time and a Manager contact for deletion if recovery is needed.

What must be built:

- A visible persistent-browser publisher that accepts only an already-approved proposal ID.
- Session/channel validation, image and caption preparation, explicit final user submission,
  scheduling timezone validation, screenshots, audit events, challenge stops, and post-verification.
- No owner password storage, stealth, CAPTCHA solving, autonomous background publishing, or path
  from the model runtime directly to the publisher.

Pass condition: one controlled post is scheduled at the intended time, appears in Qlob's scheduled
posts, matches the approved image/caption exactly, and the failure/recovery procedure is tested.

## Project-complete definition

Stages 1 through 6 are now verified within their stated boundaries. The production project is
complete only when stage 7 passes, automated checks remain green, the private GitHub `main` branch
contains the tested code, and the operating procedure can be repeated without an API key or
unreviewed publication.

See `docs/QLOB_PRODUCTION_VALIDATION.md` for the full real-data evidence.

## Official references

- [OpenAI Codex authentication](https://learn.chatgpt.com/docs/auth)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI Codex models](https://learn.chatgpt.com/docs/models)
- [YouTube channel permissions](https://support.google.com/youtube/answer/9481328?hl=en)
- [Create and schedule a YouTube Community post](https://support.google.com/youtube/answer/7124474?co=GENIE.Platform%3DDesktop&hl=en)
