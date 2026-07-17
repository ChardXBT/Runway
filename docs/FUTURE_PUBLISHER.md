# Future YouTube browser publisher

Live publishing is intentionally absent. A later `YouTubeBrowserPublisher` may implement the
existing `PostPublisher` protocol, but it must use a dedicated persistent browser profile and a
separate YouTube Editor (Limited) account. It must validate the session, prepare only an already
approved proposal, require an explicit user submission of that proposal ID, preserve screenshots
and audit events, pause on challenges, and verify the resulting scheduled post.

YouTube currently documents that Editor and Editor (Limited) roles can create and manage Community
posts. Scheduling is available from the post composer, but delegated actions can vary by surface.
Before publisher implementation, manually verify that the Qlob delegate session shows
`Create post` and `Schedule post`. Editors cannot delete published Community posts; the operating
recovery plan therefore needs a Manager for deletion.

The agent runtime must never call a publisher. Catalogue, discovery, intelligence, captions, and UI
contracts must remain unchanged. No owner credentials, stealth, CAPTCHA solving, or background
publishing may be added.
