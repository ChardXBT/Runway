# Creator platform setup

This guide covers the creator-platform features added on top of the editorial desk: connected
accounts, analytics, the content library, the inbox, and cross-platform drafts. The original
Community-post workflow (history capture, generator, Lineup, publisher) is documented in
[`OPERATIONS.md`](OPERATIONS.md) and is unchanged.

Everything here runs locally. Tokens are stored encrypted under `data/secrets/`, which Git ignores.
No platform credential is ever read from or written to the repository.

## 1. Start Runway

```powershell
npm run web:build
powershell -NoProfile -ExecutionPolicy Bypass -File .\run-runway.ps1
```

Open <http://127.0.0.1:3000/dashboard>. The old entry point <http://127.0.0.1:3000/review> still
works and is what Bot Supervisor opens.

Development: `.\.venv\Scripts\runway.exe serve` and `npm run web:dev` in two terminals.

## 2. Try it without any platform credentials

```powershell
# .env
RUNWAY_PLATFORM_PROVIDER=mock
```

Mock mode serves fictional accounts ("Northwind Studio") for YouTube, Instagram, and TikTok so every
page can be exercised and screenshotted without real data. A persistent **Demo providers** banner
is shown, `GET /api/platforms` reports `provider_mode: "mock"`, OAuth connect is disabled, and no
network calls are made. Switch back to `RUNWAY_PLATFORM_PROVIDER=live` (the default) to use real
accounts. Mock accounts are stored in the database like real ones; disconnect them from the
Accounts page if you no longer want them listed.

## 3. Configure platform apps (live mode)

Runway needs its own developer app on each platform. None of these credentials are secrets you
should share; all of them go in `.env` and nowhere else.

Common settings:

```ini
RUNWAY_PLATFORM_PROVIDER=live
# Where platforms redirect after consent. Must match what you register on each platform.
# Default: http://127.0.0.1:8000  (TikTok requires an https URL — see below.)
RUNWAY_OAUTH_REDIRECT_BASE_URL=
# Where Runway sends the browser after a successful connection. Default: http://127.0.0.1:3000
RUNWAY_WEB_BASE_URL=
```

The redirect URI for each platform is
`{RUNWAY_OAUTH_REDIRECT_BASE_URL}/api/platforms/<platform>/oauth/callback`, for example
`http://127.0.0.1:8000/api/platforms/youtube/oauth/callback`.

### YouTube (Google Cloud)

1. Create a Google Cloud project, enable **YouTube Data API v3** and **YouTube Analytics API**.
2. Configure the OAuth consent screen (External, Testing is fine for a single creator; add your
   Google account as a test user).
3. Create an OAuth client of type **Web application** and add the redirect URI above.
4. Put the client id and secret in `.env`:

```ini
RUNWAY_YOUTUBE_CLIENT_ID=
RUNWAY_YOUTUBE_CLIENT_SECRET=
# Request youtube.force-ssl as well so the inbox can post replies (off by default).
RUNWAY_YOUTUBE_ENGAGE_SCOPES=false
```

Scopes requested: `youtube.readonly`, `yt-analytics.readonly`, and `youtube.force-ssl` only when
`RUNWAY_YOUTUBE_ENGAGE_SCOPES=true`. Revenue metrics are not requested.

Quota: the default project quota is 10,000 units/day. A full sync of a channel with a few hundred
videos uses well under 100 units; each sync run reports its estimate.

### Instagram (Meta App Dashboard, Instagram API with Instagram Login)

Requires an Instagram **professional** account (Business or Creator).

1. Create a Meta app, add the **Instagram** product, choose **API setup with Instagram login**.
2. Under **Set up Instagram business login**, add the redirect URI above to the OAuth redirect
   URIs and copy the Instagram App ID and App Secret.
3. Add your Instagram account as an Instagram Tester (App roles) if the app is in development mode.

```ini
RUNWAY_INSTAGRAM_APP_ID=
RUNWAY_INSTAGRAM_APP_SECRET=
```

Scopes requested: `instagram_business_basic`, `instagram_business_manage_insights`,
`instagram_business_manage_comments`. Publishing and messaging scopes are not requested.

Alternative for your own account (Standard Access): the App Dashboard can generate a long-lived
token for accounts you added to the app. Paste it into **Accounts → Instagram → Import token**.
Runway verifies it against `GET /me`, converts it to a long-lived token if needed, and stores it
encrypted. The token itself never goes into `.env`.

### TikTok (TikTok for Developers, Login Kit + Display API)

1. Create an app at <https://developers.tiktok.com/>, add **Login Kit** and **Display API**, and
   request the scopes `user.info.basic`, `user.info.profile`, `user.info.stats`, `video.list`.
2. TikTok only accepts **https** redirect URIs, so a loopback address cannot be registered. For
   local use, run an HTTPS tunnel that forwards to the API and **rewrites the Host header to
   `127.0.0.1:8000`** (Runway only answers loopback Host headers and this protection is not
   relaxed). Examples: `cloudflared tunnel --url http://127.0.0.1:8000 --http-host-header 127.0.0.1:8000`
   or `ngrok http 8000 --host-header=rewrite`. Register
   `https://<your-tunnel-host>/api/platforms/tiktok/oauth/callback` and set
   `RUNWAY_OAUTH_REDIRECT_BASE_URL=https://<your-tunnel-host>` while connecting. The tunnel is
   only needed during the consent redirect; syncing afterwards does not need it.
3. The app must pass TikTok's review before accounts other than the app's own testers can
   authorize it. Add your account as a target user during development.

```ini
RUNWAY_TIKTOK_CLIENT_KEY=
RUNWAY_TIKTOK_CLIENT_SECRET=
```

Comments and publishing are not available through these products; see
[`PLATFORM_CAPABILITIES.md`](PLATFORM_CAPABILITIES.md).

## 4. Connect an account

Open **Accounts**. Each platform shows one of:

- **Not configured** — the `.env` values above are missing. Nothing can be connected.
- **Ready to connect** — click *Connect*. Runway opens the platform's consent page, verifies the
  identity it gets back, stores the token encrypted, and returns to Accounts.
- **Connected** — identity, granted permissions, last verification, last sync, and data freshness.
- **Needs re-authorization** — the token expired or was revoked. *Reconnect* starts a new consent
  flow for the same account; history is kept.
- **Disconnected** — the token was deleted. Imported content, metrics, and comments remain
  (read-only) until you remove the account from the database yourself.

The capability list under each account states what Runway can actually do for that account and
why something is unavailable.

## 5. Sync

Sync is manual by default. Click **Sync now** on an account, or run:

```powershell
.\.venv\Scripts\runway.exe platforms list
.\.venv\Scripts\runway.exe platforms sync            # all connected accounts
.\.venv\Scripts\runway.exe platforms sync --account 3
```

To poll automatically while the API is running:

```ini
RUNWAY_PLATFORM_SYNC_INTERVAL_MINUTES=60   # 0 (default) = manual only
```

Each run records its steps (identity, content, content metrics, daily metrics, comments), item
counts, errors, and an estimated quota cost. A failing step marks the run *partial* and the other
steps still complete. An expired token marks the account *Needs re-authorization* and stops that
account's run. Sync failures never affect the web app; pages show the last successful data with
its timestamp.

Bounds (all in `.env`): `RUNWAY_PLATFORM_SYNC_MAX_CONTENT_ITEMS` (default 200 per run),
`RUNWAY_PLATFORM_SYNC_COMMENT_CONTENT_LIMIT` (comments are fetched for the most recent N items,
default 10), `RUNWAY_PLATFORM_SYNC_ANALYTICS_DAYS` (default 90).

## 6. Inbox replies (off by default)

```ini
RUNWAY_INBOX_REPLIES_ENABLED=false
```

With the default, the inbox is read-only: browse, filter, draft, mark status, and open the
comment on the platform. Setting it to `true` enables the reply form only for accounts whose
connection includes the reply scope (`youtube.force-ssl` for YouTube,
`instagram_business_manage_comments` for Instagram). Every reply requires a confirmation, is
recorded in the action ledger, and is reported as sent only after Runway re-reads it from the
platform. If the platform response is ambiguous the action is marked *uncertain* and is never
retried automatically.

If another system already replies for the same account (for example the private Qlob Manager
project), keep this setting off to avoid two responders.

## 7. Drafts

**Drafts** plans one piece of content for several destinations with a caption per platform and
an optional date. Runway does not publish videos, Reels, or TikToks in this release: the Publish
control is disabled with the exact reason per target, and **Mark as published** records the
public URL after you post natively. When the next sync imports that post, Runway links the draft
target to the imported content so the library shows one original with its published versions.
Community posts continue to use the Lineup calendar.

## 8. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Accounts shows *Not configured* | Missing `RUNWAY_<PLATFORM>_*` values | Add them to `.env`, restart the API. |
| Consent page says redirect URI mismatch | Registered URI differs from `{RUNWAY_OAUTH_REDIRECT_BASE_URL}/api/platforms/<platform>/oauth/callback` | Register exactly that URI (watch trailing slashes). |
| Returned to Accounts with `?error=invalid_state` | The state expired (10 minutes) or was reused | Click Connect again. |
| `?error=denied` | You cancelled the consent page | Nothing stored; retry when ready. |
| Account flips to *Needs re-authorization* | Token revoked/expired and could not be refreshed | *Reconnect*. |
| Sync run *partial* with `PermissionMissing` on a step | A scope was not granted | Reconnect and grant the permission, or ignore the step. |
| Sync run *partial* with `RateLimited` | Quota or rate limit hit | Wait; YouTube quota resets at midnight Pacific. |
| TikTok connect never returns | Tunnel not rewriting Host, so the API answered 421 | Add the host-header rewrite flag to the tunnel. |
| `data/secrets/token.key` missing after a restore | Key file was not restored with the data dir | Tokens cannot be decrypted; reconnect each account. |

## 9. Supervisor compatibility

Nothing in this guide changes the Bot Supervisor contract: `run-runway.ps1`, `/health` with product
`Runway`, and the `/review` readiness route are unchanged. Supervisor can keep opening `/review`;
the new dashboard is one click away in the sidebar.
