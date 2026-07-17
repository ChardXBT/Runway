# LeeWay completion guide

LeeWay is complete as a local fixture-tested planning application. It is not yet complete as a
production Qlob posting system because the real Qlob data, live discovery, delegate UI, and
YouTube scheduler have not been proven.

## 1. Prove delegate access before building the Qlob database

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

## 3. Build the real Qlob database

What the user provides:

- The same delegate Google account signed into LeeWay's dedicated visible browser profile.
- Manual handling of consent or challenges.
- Time to leave the browser open during the resumable read-only capture.

Run:

```powershell
.\.venv\Scripts\leeway.exe capture youtube-posts --channel-url "https://www.youtube.com/@Qlob/posts" --headed --resume
.\.venv\Scripts\leeway.exe catalog verify
```

Pass condition: expected historical posts and media are present, duplicates are absent, source
URLs are retained, and the verification report has no unexplained integrity failures.

## 4. Build and evaluate Qlob style retrieval

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

Pass condition: representative profile examples look like Qlob, holdout results are acceptable,
and a human review finds no recurring annotation error that would distort search or captions.

## 5. Prove image discovery and safeguards

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

Pass condition: candidates have working provenance links, low-quality/duplicate/artwork candidates
are rejected, and the review set is useful. A caption does not by itself establish permission to
reuse an image, so rights marked `unknown` still require human judgment.

## 6. Prove captions and review UI

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

Pass condition: a human can reliably select a suitable image/caption pair for each day, rejected
patterns affect later retrieval, and no item reaches scheduling without approval.

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

The production project is complete only when all seven stages pass, automated checks are green,
the private GitHub `main` branch contains the tested code, and the operating procedure can be
repeated without an API key or unreviewed publication.

## Official references

- [OpenAI Codex authentication](https://learn.chatgpt.com/docs/auth)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI Codex models](https://learn.chatgpt.com/docs/models)
- [YouTube channel permissions](https://support.google.com/youtube/answer/9481328?hl=en)
- [Create and schedule a YouTube Community post](https://support.google.com/youtube/answer/7124474?co=GENIE.Platform%3DDesktop&hl=en)
