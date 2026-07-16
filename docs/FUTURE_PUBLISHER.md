# Future YouTube browser publisher

Live publishing is intentionally absent. A later `YouTubeBrowserPublisher` may implement the
existing `PostPublisher` protocol, but it must use a dedicated persistent browser profile and a
separate YouTube Editor (Limited) account. It must validate the session, prepare only an already
approved proposal, require an explicit user submission of that proposal ID, preserve screenshots
and audit events, pause on challenges, and verify the resulting scheduled post.

The agent runtime must never call a publisher. Catalogue, discovery, intelligence, captions, and UI
contracts must remain unchanged. No owner credentials, stealth, CAPTCHA solving, or background
publishing may be added.
