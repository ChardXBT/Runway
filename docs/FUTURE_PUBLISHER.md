# YouTube publisher and persisted outbox

The filename is retained for existing links; the publisher is implemented.

## Normal editorial path

```text
needs_review
  -> approved (caption/image decision and daily slot reservation)
  -> internally_scheduled
  -> queued
  -> submitting
  -> externally_scheduled OR publish_unverified OR publish_failed
```

`Accept` is the human authorization for that exact proposal. The API immediately
returns the next review option while a single background worker processes the persisted FIFO
outbox. A model runtime cannot call this path.

Each approved item is assigned the first unreserved 10:00 AM `America/Toronto` slot. The scheduler
has no fixed horizon and enforces one RunWay-generated post per local day.

## Interlocks

- `RUNWAY_PUBLISHING_ENABLED=false` blocks all external scheduling.
- The browser uses a dedicated persistent profile under `data/browser-profile/publisher`.
- RunWay validates the configured Qlob channel and Editor access before composer interaction.
- The exact image bytes, caption, time, proposal ID, and channel ID are hashed.
- Known-blocked candidates and missing media cannot enter the outbox.
- Only one submission can run at a time; queue attempts are idempotent and persisted across restarts.
- An expired Google session stops before the composer, marks the outbox paused, and can be resumed
  after the operator signs in.
- Once the final YouTube Schedule click may have occurred, an ambiguous result is verification-only
  and is never automatically retried.
- Before/after/failure screenshots and audit events preserve the evidence.

The older CLI `publisher prepare` / `publisher confirm` flow remains available for diagnostics,
but the product UI does not require a token or typed phrase. The explicit `Accept`
button is the normal proposal-specific confirmation.
