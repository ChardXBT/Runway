# Runway operating and completion guide

Runway is a continuous Qlob editorial conveyor. It retrieves from the local Qlob history, finds and
ranks images, proposes question-first captions, learns from every decision, and schedules approved
posts through a visible YouTube browser.

## The normal session

1. Open `/review`.
2. Inspect the feed preview and caption.
3. Optionally edit the caption or choose an alternative.
4. Choose one action:
   - `Accept` records positive evidence, assigns the next free 10:00 AM Eastern day,
     enters the post in the persisted YouTube outbox, and opens the next option.
   - `Reject` records the complete image/caption option as negative evidence and opens the next option.
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

## Scheduling architecture

Approval performs these local operations in order, with a durable checkpoint at each state:

```text
needs_review
  -> next free daily slot assigned
  -> approved
  -> internally_scheduled
  -> persisted YouTube outbox
```

The UI advances after the outbox entry is durable; it does not wait for browser automation. A
single worker processes the outbox in FIFO order, so multiple rapid approvals cannot launch
simultaneous publisher browsers.

The worker validates the saved Qlob Editor session, fills the exact image/caption/date/time, clicks
Schedule once, captures screenshots, and verifies the Scheduled tab. Any preflight or browser error
pauses the outbox. A possibly submitted post enters `publish_unverified` and is never automatically
resubmitted.

## Publisher setup

```powershell
.\.venv\Scripts\runway.exe publisher login
.\.venv\Scripts\runway.exe publisher status
```

Use the Google account YouTube identifies as a Qlob Editor. The dedicated publisher profile remains
local under the ignored data directory.

Enable the local feature gate:

```dotenv
RUNWAY_PUBLISHING_ENABLED=true
RUNWAY_PUBLISHER_CHANNEL_ID=UCQ-nHijGwxNU3Go_wyLQ5Ng
```

Restart Runway. The primary navigation should read `Auto-schedule on · One bot post daily`.

The legacy CLI prepare/confirm commands remain available for diagnostics, but the normal product
flow uses the explicit `Accept` action as the human scheduling instruction.

## Completion definition

Software completion requires:

- migration from an empty database and upgrade of the production database;
- uncapped next-slot allocation with one Runway post per local date;
- automatic positive/negative feedback capture;
- a persistent serial publisher outbox with pause-on-error behavior;
- desktop and mobile conveyor verification;
- backend tests, strict typing, lint, format, frontend tests, production build, and audits;
- private GitHub `main` synchronized with green CI.

Account-specific YouTube acceptance is complete only after one real approved option appears in
Qlob’s Scheduled tab with the exact image, caption, date, and time.
