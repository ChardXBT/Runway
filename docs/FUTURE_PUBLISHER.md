# YouTube publishing modes and persisted outbox

The filename is retained for existing links; the publisher is implemented.

## Normal product path

```text
needs_review
  -> approved (caption/image decision and daily slot reservation)
  -> internally_scheduled
  -> editable local Lineup
  -> explicit confirmed Lineup action
       assisted: validated native workspace only
       authorized_browser: queued -> submitting
         -> externally_scheduled OR publish_unverified OR publish_failed
```

`Accept` is an editorial decision, not authorization to mutate YouTube. It records the existing
feedback, adds the item to Lineup, returns the next review option, and creates no publisher attempt.
A model runtime cannot call the publisher path.

Each approved item starts at the first unreserved default 10:00 AM `America/Toronto` slot. Its date
and time remain editable. The scheduler has no fixed horizon and currently enforces one
Runway-generated post per local day as a product policy.

## Interlocks

- `assisted` is the default and never queues or opens a browser.
- `authorized_browser` requires mode selection, `RUNWAY_PUBLISHING_ENABLED=true`, and
  `RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=true`; an invalid combination fails at startup.
- The browser uses a dedicated persistent profile under `data/browser-profile/publisher`.
- Runway validates the configured Qlob channel and observed Community-post controls before
  composer interaction. The owner declares the delegated role; YouTube does not return it here.
- The exact image bytes, caption, time, proposal ID, and channel ID are hashed.
- Known-blocked candidates and missing media cannot enter the outbox.
- Only one submission can run at a time; queue attempts are idempotent and persisted across restarts.
- An expired Google session stops before the composer, marks the outbox paused, and can be resumed
  after the operator signs in.
- Once the final YouTube Schedule click may have occurred, an ambiguous result is verification-only
  and is never automatically retried.
- Before/after/failure screenshots and audit events preserve the evidence.

The older CLI `publisher prepare` / `publisher confirm` flow remains available for diagnostics.
The confirmed Lineup action is the only normal entry point. A batch is not externally atomic, and
queue creation is never presented as verified scheduling.
