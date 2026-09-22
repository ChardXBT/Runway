# Capture safety and operations

Capture is a one-time, user-initiated read-only operation. Managed mode opens a visible persistent
Chromium context in `data/browser-profile/`; CDP mode connects only to an endpoint explicitly
provided by the user. Runway never requests a password, reads an ordinary browser profile, or
clicks create/edit/delete/publish/schedule/account/security controls.

Before scrolling, the adapter checks the requested channel Posts/Community surface. It pauses for
authentication, consent, account selection, CAPTCHA, unexpected navigation, or low selector
confidence. `Ctrl+C` stops after the current transaction; rerun with `--resume`. Errors save an
HTML snapshot, screenshot, and JSON diagnostic under `data/snapshots/`.

Use the canonical Qlob channel-ID route:

```powershell
.\.venv\Scripts\runway.exe capture youtube-posts --channel-url "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx/posts" --headed --resume
```

YouTube can pause at continuation boundaries or replace continuation elements while they are being
observed. Runway therefore ignores layout-height jitter, retries detached continuation renderers,
uses bounded scroll/bounce probes, and declares the surface complete only after both 30 unchanged
probes and 90 sustained seconds with no new card count or tail post ID. An explicit live
`--resume` can reopen the latest completed checkpoint without duplicating post IDs.

An already controlled local browser agent can checkpoint immutable card DOM through
`POST /api/capture/browser-checkpoint`. This bridge is loopback-only, requires the explicit
`X-Runway-Capture-Source: browser-agent` header, accepts at most ten cards and 4 MiB per request,
accepts only HTTPS YouTube channel URLs, and feeds the same idempotent capture run. It cannot
publish. Finalization additionally requires the stored post count, independently observed surface
count, and final tail post ID to agree exactly.

Fixture mode is the only capture path used by setup and automated tests.
