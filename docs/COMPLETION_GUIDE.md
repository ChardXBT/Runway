# Runway operating and completion guide

Runway is a continuous Qlob editorial conveyor. It retrieves from the local Qlob history, finds and
ranks images, proposes question-first captions, learns from every decision, and manages an editable
local Lineup with explicit assisted or separately authorized external handling.

## The normal session

1. Open `/review`.
2. Inspect the feed preview and caption.
3. Optionally edit the caption or choose an alternative.
4. Choose one action:
   - `Accept` records positive evidence, assigns the next free default Lineup slot, performs no
     YouTube action, and opens the next option.
   - `Reject` records the complete image/caption option as negative evidence and opens the next option.
   - `Fewer like this` rejects the current look with an explicit `too_similar` image-cluster signal,
     suppressing visually related candidates in later ranking.
5. Continue for as many options as desired. There is no schedule-horizon cap.

The schedule rule applies only to Runway: at most one bot post per `America/Toronto` local date.
Manually created Qlob posts are independent.

## What Runway learns

Runway does not alter Codex model weights. It adapts immediately through local retrieval:

- Qlob history supplies tone, caption structures, visual patterns, and novelty evidence.
- Every caption pass requests four open questions, three observations, and two reactions.
- A deterministic reranker prioritizes grounded `why`/`how`/`what` questions.
- If the operator changes a caption before approval, the generated caption becomes negative
  evidence and the edited caption becomes a preferred example.
- Approval records positive caption and image evidence.
- Rejection records a negative option signal.
- A rejection records both caption and image feedback so a weak pairing is less likely to return.
- The next caption pass retrieves the most relevant positive and negative examples.

No paid OpenAI API, local GPU, or weight-training job is required. Codex uses ChatGPT-plan
authentication and stops when included usage is unavailable.

## Keeping the feed supplied

Runway first converts unused accepted candidates into review options. When none remain, `Find more
options` opens the configured visible discovery browser, downloads candidates, applies hard safety
and duplicate filters, analyzes them with Codex, and returns accepted options to the tray. Search
challenges are never bypassed.

Source URLs and source metadata remain in the local archive. They do not block the fast approval
path.

## Scheduling and publishing architecture

Approval performs these local operations in order, with a durable checkpoint at each state:

```text
needs_review
  -> next free daily slot assigned
  -> approved
  -> internally_scheduled
  -> editable local Lineup
```

The UI advances after local scheduling is durable. Accept, caption/time edits, moves, swaps, and
local removal never create a publisher attempt, validate a session, or open Chrome. Each Lineup
item stores a complete timezone-aware timestamp; the default time is only the initial suggestion.

Only the confirmed Lineup publishing action crosses the external boundary. Default assisted mode
validates exact media/caption/timestamp/rights fields and returns an ordered native posting
workspace without queue or browser work. Its completion controls are session-local and do not mark
external verification.

Separately authorized browser mode creates the existing serial FIFO outbox. The worker validates
observed Qlob posting capability, fills one exact image/caption/date/time at a time, clicks Schedule
once, captures screenshots, and verifies the Scheduled tab. Any preflight or browser error pauses
the outbox. A possibly submitted post enters `publish_unverified` and is never automatically
resubmitted. Items in a requested batch are verified independently; the batch is not atomic.

Settings reads the last durable publisher check without opening Chrome. A successful result becomes
stale after 24 hours. `Check saved session` performs the fresh read-only browser capability check and
records its individual channel, identity, posting-access, and composer results.

Profile reports training readiness separately for caption, image, and pairing. A target becomes
trainable only when it has at least eight training-eligible human pairwise labels; held-out study
labels do not inflate this threshold.

## Publisher setup

```powershell
.\.venv\Scripts\runway.exe publisher login
.\.venv\Scripts\runway.exe publisher status
```

The channel owner may invite the configured connector account as `Editor (Limited)`, then use that
Google identity for publisher login. This is an owner-declared role. Runway observes whether the
saved session exposes the expected channel and posting controls; it cannot query the exact role.
The dedicated publisher profile remains local under the ignored data directory.

Assisted mode is the safe default:

```dotenv
RUNWAY_PUBLISHING_MODE=assisted
RUNWAY_PUBLISHING_ENABLED=false
RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=false
RUNWAY_PUBLISHER_CHANNEL_ID=UCQ-nHijGwxNU3Go_wyLQ5Ng
```

Authorized browser operation requires all three interlocks:

```dotenv
RUNWAY_PUBLISHING_MODE=authorized_browser
RUNWAY_PUBLISHING_ENABLED=true
RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=true
```

The legacy CLI prepare/confirm commands remain available for diagnostics, but the normal product
flow uses the explicit confirmed Lineup action.

## Completion definition

Software completion requires:

- migration from an empty database and upgrade of the production database;
- uncapped next-slot allocation with one Runway post per local date;
- automatic positive/negative feedback capture;
- a persistent serial publisher outbox with pause-on-error behavior;
- local-only Generator acceptance and Lineup mutation isolation;
- a default assisted workspace that needs no valid publisher login;
- desktop and mobile conveyor verification;
- backend tests, strict typing, lint, format, frontend tests, production build, and audits;
- private GitHub `main` synchronized with green CI.

Account-specific YouTube acceptance is complete only after one real approved option appears in
Qlob’s Scheduled tab with the exact image, caption, date, and time.

This publishing refactor intentionally changes no intelligence/ML behavior, SQLAlchemy schema,
Alembic migration, database engine, or production data.

See `docs/CURRENT_LIMITATIONS.md` for browser/API constraints, connector proof boundaries, model
activation requirements, analytics gaps, and the work required before multi-user signup is honest.
