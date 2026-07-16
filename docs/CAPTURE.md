# Capture safety and operations

Capture is a one-time, user-initiated read-only operation. Managed mode opens a visible persistent
Chromium context in `data/browser-profile/`; CDP mode connects only to an endpoint explicitly
provided by the user. Leeway never requests a password, reads an ordinary browser profile, or
clicks create/edit/delete/publish/schedule/account/security controls.

Before scrolling, the adapter checks the requested channel Posts/Community surface. It pauses for
authentication, consent, account selection, CAPTCHA, unexpected navigation, or low selector
confidence. `Ctrl+C` stops after the current transaction; rerun with `--resume`. Errors save an
HTML snapshot, screenshot, and JSON diagnostic under `data/snapshots/`.

Fixture mode is the only capture path used by setup and automated tests.
