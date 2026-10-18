# Security

- Services bind only to loopback. Binding does not stop a page in the operator's own browser
  from reaching them, so both servers also check the request:
  - The API and the web server only serve loopback `Host` headers (`127.0.0.1`, `localhost`,
    `[::1]`), which defeats DNS rebinding.
  - API requests other than `GET`/`HEAD`/`OPTIONS` are refused when the browser reports a foreign
    `Origin` or a cross-site fetch. This covers body-less actions such as approve or queue resume
    that a cross-site form could otherwise trigger. The CLI and server-side calls send neither
    header and are unaffected.
  - The web app sends `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, and
    `no-referrer`, so the review and Lineup controls cannot be framed by another site.
- Third-party image URLs (search results, manual URLs, captured posts) are fetched by
  `runway.media.download`. Only HTTP(S) on ports 80/443 without embedded credentials is
  accepted. Every hop, including each redirect (at most three, no HTTPS-to-HTTP downgrade), is
  resolved once and refused unless every address is public; the connection is then pinned to
  that address while TLS still verifies the original hostname. Proxy environment variables are
  ignored. Bodies are streamed under `RUNWAY_MAXIMUM_IMAGE_BYTES` and an overall deadline,
  and must decode as JPEG, PNG, WebP, or GIF within `RUNWAY_MAXIMUM_IMAGE_PIXELS` (default 40
  megapixels) before anything is written to disk.
- `.env`, databases, media, snapshots, and browser profiles are ignored by Git.
- No Google password or owner credential is requested or stored.
- Live browser adapters are headed, explicit, challenge-aware, and contain no stealth/evasion.
- The local browser-agent capture bridge accepts only bounded HTTPS YouTube DOM checkpoints,
  requires an explicit source header, and validates exact count/tail agreement before completion.
  It has no publishing capability.
- External images retain provenance and default to unknown rights status.
- Search planning excludes fan art, personal artwork, portfolios, commissions, and independent
  illustrations. Model-detected fan art or personal artwork is warned at probability `> 0.25` and
  hard-rejected at probability `>= 0.50`.
- Codex must be authenticated through `Sign in with ChatGPT`. API-key authentication is rejected.
- Codex subprocesses receive no environment variable whose name looks like a credential
  (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `COOKIE`), which covers `OPENAI_API_KEY`,
  `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, and `RUNWAY_SEARCH_API_KEY`. They run ephemerally in an
  isolated temporary directory, use a read-only sandbox, never request
  approvals, and have web search disabled.
- Model jobs are serialized to avoid accidental parallel allowance consumption. Authentication,
  usage-limit, timeout, and malformed-output failures stop the batch; no paid API fallback exists.
- Model output is schema validated, treated as data, and never executed as SQL or a command.
- Generator acceptance is local-only and cannot create queue or browser work. Default assisted
  publishing validates media, current rights fields, caption, and future timezone-aware timestamp
  without requiring a browser session. Authorized browser work requires an explicit confirmed
  Lineup action plus mode, enablement, and authorization-acknowledgement interlocks. It also
  requires an observed Qlob posting capability and an unchanged payload hash.
- Google sign-in occurs manually in ordinary installed Chrome with an isolated
  Runway profile. Playwright is not active during credential entry. Runway does
  not bypass Google warnings, reduce account protections, export cookies, or
  automate passwords or multifactor challenges.
- Outbox attempts are persisted and serialized. A stale login pauses before composer interaction;
  resume is explicit after sign-in. Google credentials and cookies remain inside the ignored
  dedicated browser profile.
- A final Schedule click is marked possibly submitted before it is attempted. Ambiguous failures
  enter a verification-only state, preventing automatic duplicate retries.
- Unsubmitted Lineup items may be edited or removed locally; active payload attempts are
  superseded. Externally confirmed posts are immutable in normal Lineup actions.
- Assisted responses expose media URLs under `/media/`, never local filesystem paths, browser
  profiles, cookies, or session material.

## Creator platform connections

- Platform accounts connect through each platform's official OAuth 2.0 flow (Google, Business
  Login for Instagram, TikTok Login Kit). Runway never asks for a platform password.
- Each authorization issues a random single-use `state` stored in `oauth_states` with a
  ten-minute expiry; the callback rejects unknown, reused, expired, or platform-mismatched
  states and redirects back to `/accounts` with a short error code only. Google requests use
  PKCE (S256). Consent requests the minimum scopes: read-only data and analytics; the YouTube
  `force-ssl` scope is requested only when `RUNWAY_YOUTUBE_ENGAGE_SCOPES=true`.
- Access and refresh tokens are stored in `data/secrets/platform-tokens.json`, encrypted with
  Fernet using a key in `data/secrets/token.key` (created with mode `0600` on POSIX). The
  database holds only an opaque `token_ref`, so database backups contain no usable credential.
  `data/secrets/` is ignored by Git. Disconnecting revokes the token where the platform supports
  it and deletes it locally.
- Tokens are sent only as `Authorization: Bearer` headers to the platform hosts, never in URLs,
  frontend responses, logs, or persisted JSON; `redact_payload` strips token-like fields from
  anything written to `platform_actions`.
- The OAuth callback is an ordinary top-level `GET` on the loopback API, so the existing
  `LocalRequestGuard` still applies: only loopback `Host` headers are served. TikTok requires
  an HTTPS redirect, which means a tunnel that rewrites the Host header; the guard is not
  relaxed for it.
- Comment replies are the only external write. They require
  `RUNWAY_INBOX_REPLIES_ENABLED=true`, a connected account holding the reply scope, and a
  per-reply confirmation. Every reply is recorded in `platform_actions` before it is attempted
  and is reported as sent only after Runway re-reads it from the platform. Ambiguous outcomes
  are marked `uncertain` and never retried automatically.
- Mock providers run only when `RUNWAY_PLATFORM_PROVIDER=mock`; they make no network calls,
  cannot be connected through OAuth, and are labelled in the UI.
- Sync reads are bounded per run (items, pages, comment targets) and back off on platform
  rate-limit errors. A provider outage degrades a sync run, not the web app.

## Dependency audits

CI runs `pip-audit`, a strict `npm audit --omit=dev` for the web app, and
`scripts/audit_web_dependencies.mjs` for the full web tree. The script fails on any finding that
is not recorded in `apps/web/audit-exceptions.json`, reachable from production dependencies, or
past its expiry date, and it prints each accepted exception on every run.

One exception is currently recorded: `braces` (GHSA-vfj7-8cjw-p6xm, stack exhaustion through
deeply nested patterns), which has no patched release. It reaches Runway only through
`eslint-config-next > @next/eslint-plugin-next > fast-glob > micromatch`, a development-only lint
chain that is absent from the production install. The plugin globs only when ESLint
`settings.next.rootDir` is set, which Runway's configuration does not do, so the vulnerable
code receives no input. The only fix npm offers is a downgrade to `eslint-config-next` 14. The
exception expires on 2027-01-31 and must be reviewed again then, or earlier if a patched
release appears.
