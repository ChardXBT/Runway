# Guarded YouTube browser publisher

The former future-publisher milestone is implemented as `YouTubeBrowserPublisher`.

## Boundary

The publisher uses a dedicated persistent Chromium profile at
`data/browser-profile/publisher`. Google credentials are entered only in the visible browser;
LeeWay does not request a password, export cookies, solve challenges, use stealth, or run a hidden
background publisher.

The caption/model pipeline has no route into the publisher. A proposal must pass every state
boundary:

```text
needs_review
  -> approved (human caption/image decision + provenance decision)
  -> internally_scheduled
  -> prepared (no YouTube submission)
  -> publishing (one-time confirmation consumed)
  -> externally_scheduled OR publish_unverified OR publish_failed
```

## Interlocks

- `LEWAY_PUBLISHING_ENABLED=false` blocks session launch, preparation, and confirmation.
- The channel URL is pinned to Qlob's configured channel ID.
- The visible page must expose the Qlob heading, `You're an editor`, and one Community composer.
- Preparation accepts only an internally scheduled proposal with local media, a future timezone-aware
  time, and a recorded provenance decision.
- Preparation creates a short-lived random token. Only its SHA-256 hash is stored.
- The image bytes, caption, time, proposal ID, and channel ID are hashed. Any change invalidates the
  attempt.
- Final submission requires both the one-time token and the exact phrase
  `SCHEDULE QLOB #<proposal-id>`.
- The browser captures before/after/failure screenshots and checks Qlob's Scheduled tab.
- Once the final Schedule click is attempted, any failure is treated as possibly submitted.
  LeeWay enters `publish_unverified` and offers verification, never an automatic retry.
- In-process confirmation locking and persisted attempt state prevent token reuse and parallel
  duplicate submissions.

## Acceptance status

All interlocks, state transitions, expiry, token reuse, payload tampering, successful verification,
and ambiguous-submission recovery are covered by offline fake-adapter tests. The Qlob Editor
composer/channel contract was inspected read-only in a signed-in browser. A real Schedule click was
not part of automated or visual QA and still requires a separate, proposal-specific user
authorization.
