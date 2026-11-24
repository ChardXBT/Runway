# Connect your platform accounts

The owner's runbook for connecting YouTube, Instagram and TikTok to a local Runway install.
Everything below is a numbered list of things to do in order. Steps only you can do — creating
developer apps, accepting terms, running consoles — are marked **[You]**.

At any point, `runway doctor platforms` tells you what is set, what each connected account can
do, and exactly what is still missing. It never prints secrets, makes no network call unless
you pass `--check-edge`, and writes nothing to disk. It exits `0` when everything is ready and
`1` while the "What is missing" list is not empty.

```powershell
.\.venv\Scripts\runway.exe doctor platforms
.\.venv\Scripts\runway.exe doctor platforms --json        # same report for machines
.\.venv\Scripts\runway.exe doctor platforms --check-edge  # also pings your public edge
```

Facts here come from `docs/expansion/research/` (dated 2026-11-14, plus the
`recheck-20261122.md` and `recheck-20261123.md` passes). Console
labels are quoted as recorded there; where the research did not record a label this runbook
says "the console's credentials page" instead of guessing. Re-check each console before you
trust the wording — these pages change.

---

## 1. Prerequisites

1. **Private instance** (recommended): platform client secrets belong in
   `secrets.enc.env`, not in the checkout's `.env`. Follow `docs/PRIVATE_INSTANCE.md`
   through `runway instance init`, then set `RUNWAY_INSTANCE_DIR` and keep `sops` and `age`
   installed. The doctor's "Private instance" line reports whether this machine is `active`
   (holds the lease), `standby`, or not configured, and whether credentials actually come
   from `secrets.enc.env`. Connecting and token storage are active-machine writes — on a
   standby machine the doctor puts the lease line in "What is missing".
2. **Public edge** (required for TikTok, optional otherwise): a tunnel that gives the local
   API an HTTPS URL for **two path prefixes only**: `/hooks/*` (webhooks and the health
   check) and `/api/platforms/*/oauth/callback` (OAuth redirects). Tailscale Funnel is the
   verified option — its proxy keeps the `Host` header and adds `Tailscale-Funnel-Request`,
   which Runway's edge guard uses to refuse every other path. Cloudflare Tunnel or an
   equivalent works the same way. **[You]** For example, with the API on port 8000:

   ```powershell
   tailscale serve --bg --set-path=/hooks http://127.0.0.1:8000/hooks
   tailscale serve --bg --set-path=/api/platforms http://127.0.0.1:8000/api/platforms
   tailscale funnel on
   tailscale serve status
   ```

   Each target URL repeats the mount path because Serve strips the `--set-path`
   prefix before proxying and appends what is left to the target URL — the prefix
   has to live on the target for the request to reach the real route. One
   `/api/platforms` mount is enough: Runway's edge guard still serves edge traffic
   only the exact `/api/platforms/{platform}/oauth/callback` pattern. Never mount
   `/` — the public edge must offer only these two prefixes. The status command
   should list exactly the two mounts above. Then set both settings to the same
   host:

   ```dotenv
   RUNWAY_PUBLIC_EDGE_URL=https://<machine>.<tailnet>.ts.net
   RUNWAY_OAUTH_REDIRECT_BASE_URL=https://<machine>.<tailnet>.ts.net
   ```

   - `RUNWAY_PUBLIC_EDGE_URL` tells the webhook subscriptions where to reach you and lets
     `runway doctor platforms --check-edge` probe `https://<edge>/hooks/health`.
   - `RUNWAY_OAUTH_REDIRECT_BASE_URL` is the base Runway builds each redirect URI from:
     `{base}/api/platforms/{platform}/oauth/callback`. The doctor prints the exact URI to
     register in each console — register it verbatim.
   - Without an edge, YouTube and Instagram still work on loopback polling; TikTok connect
     cannot, because TikTok requires an HTTPS redirect URI.
3. **Optional: Cloudflare R2** for Instagram image/carousel publishing. Instagram Login
   requires a **publicly accessible media URL** for `image_url`/`video_url`/`cover_url` —
   it fetches the file itself. A small R2 bucket with presigned URLs covers this. Set
   `RUNWAY_R2_ACCOUNT_ID`, `RUNWAY_R2_BUCKET`, `RUNWAY_R2_ACCESS_KEY_ID`,
   `RUNWAY_R2_SECRET_ACCESS_KEY` (and `RUNWAY_R2_ENDPOINT` only if yours is non-default).
   Without it, `instagram_image`/`instagram_carousel` report `needs_media_host` while Reels
   keep working — and the manual workflow needs nothing.

---

## 2. Google Cloud (YouTube)

**[You]** All in <https://console.cloud.google.com>:

1. Create **one** Google Cloud project for Runway (the developer policy requires exactly
   one API project per client; a separate upload project is allowed later if ever needed).
2. Enable **YouTube Data API v3** and **YouTube Analytics API**.
3. OAuth consent screen: user type **External**. Add your Google account as a test user
   while you set up.
4. Set the publishing status to **In production** but **do not** submit for verification.
   Why: a project left in **Testing** issues refresh tokens that die after **7 days** —
   you would reconnect every week. "In production" without verification shows an
   "unverified app" warning once at consent, and Google documents a 100-user cap
   for unverified apps — fine for your own account. (Whether unverified "In
   production" really avoids the 7-day expiry — and how the documented cap
   applies — is acceptance test Y-1; the research flags it as likely-but-unproven.)
5. On the console's credentials page create an OAuth client of type **Web application**.
6. Add the redirect URI `runway doctor platforms` prints for `youtube` — by default
   `http://127.0.0.1:8000/api/platforms/youtube/oauth/callback`. Google exempts loopback
   `http` from its HTTPS rule, so local works without a tunnel. If you set
   `RUNWAY_OAUTH_REDIRECT_BASE_URL` to the edge, register that https URI instead.
7. Put the credentials where secrets live (instance `secrets.enc.env` preferred,
   `.env` acceptable on a machine that has no private instance):

   ```dotenv
   RUNWAY_YOUTUBE_CLIENT_ID=
   RUNWAY_YOUTUBE_CLIENT_SECRET=
   ```

Scopes Runway asks for at connect time (the consent screen lists them): `youtube.readonly`
and `yt-analytics.readonly` — plus `youtube.force-ssl` only when
`RUNWAY_YOUTUBE_ENGAGE_SCOPES=true` (comment replies). The separate **enable publishing**
consent on the Accounts page adds `youtube.upload`, and `youtube.force-ssl` too once the
posts-API pilot applies (`RUNWAY_YOUTUBE_POSTS_API_ACCESS=true`).

---

## 3. Meta (Instagram)

Prerequisite: the Instagram account must be a **Professional** account (Creator or
Business). Switch it in the Instagram app first if it is still personal.

**[You]** In the Meta developer dashboard (<https://developers.facebook.com>):

1. Create a Meta developer app.
2. Add the Instagram product and choose **API setup with Instagram login** (the default
   "Instagram API with Instagram Login" path).
3. Add your Instagram account as an **Instagram tester** on the app (a Meta app in
   development mode only works for accounts that hold a role on it; up to 50 testers are
   allowed). Alternatively generate a token on the console's credentials page and import
   it — see step 5.
4. Register the redirect URI the doctor prints for `instagram`. Meta requires **https** —
   so this is the public-edge URL `https://<edge>/api/platforms/instagram/oauth/callback`,
   not a loopback address.
5. Credentials:

   ```dotenv
   RUNWAY_INSTAGRAM_APP_ID=
   RUNWAY_INSTAGRAM_APP_SECRET=
   RUNWAY_INSTAGRAM_WEBHOOK_VERIFY_TOKEN=   # any string you choose; needed for webhooks
   ```

   The verify token is the value you paste into the webhook subscription form so Meta's
   verification GET echoes `hub.challenge` only for you. The Instagram webhook URL is
   `https://<edge>/hooks/instagram`.
6. Alternative: **Unified Login** ("API setup with Unified login", found under the app's
   Instagram use case — the research records the path as *Use cases > Manage messaging &
   content on Instagram > Customize > API setup with Unified login*). This is the Facebook
   Login for Business path: it needs a free business portfolio and gives Business scope
   names without `_business` and resumable upload from disk. Connect it with
   **Connect with Facebook Login (Unified)** on the Accounts page (the
   `instagram_unified` adapter) and set:

   ```dotenv
   RUNWAY_META_APP_ID=
   RUNWAY_META_APP_SECRET=
   RUNWAY_INSTAGRAM_UNIFIED_CONFIG_ID=      # the config_id of the Login configuration
   ```

   Either credential set fully configures Instagram for connect; the doctor reports both.
   Note `meta_app_*` and the config id can only come from the environment or `.env` —
   `secrets.enc.env` does not carry them today.
7. If there is no HTTPS edge at all you can still connect: generate a token in the
   console's credentials page and use the **import token** form on the Accounts page.
   The doctor notes this path whenever the redirect URI fails the https rule.

Scopes: the connect consent asks for `instagram_business_basic`,
`instagram_business_manage_insights`, `instagram_business_manage_comments`; the
enable-publishing consent adds `instagram_business_content_publish`. A Unified Login
grant satisfies the same needs under its own names (`instagram_basic`,
`instagram_content_publish`, …) — Runway treats them as equivalent.

---

## 4. TikTok

TikTok requires an HTTPS redirect and a reachable webhook URL, so finish the public edge
in §1 first.

**[You]** In the TikTok developer portal (<https://developers.tiktok.com>):

1. Create a developer app. Add all three products: **Login Kit** and **Display
   API** (the connect scopes `user.info.*` and `video.list` are Display API
   scopes — Login Kit alone does not grant them), plus the **Content Posting
   API** with the **upload (inbox)** configuration for publishing.
2. Create a **sandbox** on the app and add your TikTok account as a **target user** (up to
   5 sandboxes, 10 target users each; no review needed).
3. Register the redirect URI the doctor prints for `tiktok`:
   `https://<edge>/api/platforms/tiktok/oauth/callback`.
4. Register the webhook callback `https://<edge>/hooks/tiktok` (one callback URL per app;
   it receives every event).
5. Credentials:

   ```dotenv
   RUNWAY_TIKTOK_CLIENT_KEY=
   RUNWAY_TIKTOK_CLIENT_SECRET=
   ```

Scopes: connect asks for `user.info.basic`, `user.info.profile`, `user.info.stats`,
`video.list`. Enable publishing adds `video.upload` (inbox drafts). `video.publish`
(direct public posts) is asked for **only** when `RUNWAY_TIKTOK_APP_STATUS=audited` —
TikTok refuses it otherwise, and by its own rules an owner-only utility cannot pass the
direct-post audit. Plan on the inbox-draft path: Runway uploads the video, TikTok sends
you an inbox notification, and you finish the post in the app.

Tokens: TikTok access tokens live about 2 hours; refresh tokens about 180 days and
**rotate** — every refresh returns a different refresh token. Runway handles the rotation
for you; the doctor reads `expires_at`/`refresh_expires_at` from the stored token and
says `refresh_expired` when the 180 days are gone.

---

## 5. Connect and run the doctor until it says ready

1. Start Runway: `powershell -NoProfile -ExecutionPolicy Bypass -File .\run-runway.ps1`.
2. Open the Accounts page: <http://127.0.0.1:3000/accounts>.
3. Click **Connect YouTube** / **Connect Instagram** / **Connect TikTok**, complete the
   consent on the platform's site, and land back on Accounts. For Unified Login use
   **Connect with Facebook Login (Unified)** (the `instagram_unified` adapter); when the
   token covers several Instagram accounts the page asks which one to connect.
4. On each connected account open **Freshness & publishing** and click
   **Enable publishing** — that runs the second, incremental consent that adds the upload
   scope. Without it the account syncs but cannot post.
5. `runway doctor platforms` after each step. It lists, per platform: each credential
   (set? from environment / `.env` / `instance.toml` / `secrets.enc.env` — never the
   value; `RUNWAY_INSTAGRAM_WEBHOOK_VERIFY_TOKEN` is reported set/unset only, it never
   gates "configured"), the redirect URI to register, the requested scopes versus the
   scopes each account actually holds, the token state (`valid`, `refreshable`,
   `expired`, `refresh_expired`, `unreadable`, `missing`), the app-status setting, the
   per-post-type capability with its reason, and a "What is missing" to-do list. Exit
   code is `1` until the list is empty. The token state `unreadable` means the stored
   token could not be read (the reason is printed) — the doctor reads tokens but never
   writes `token.key` or any other file to find out.
6. The same report is available to the web app as `GET /api/v1/doctor/platforms`
   (read-only, owner only).

---

## 6. Turning on publishing

Publishing has several deliberate gates; the doctor's "Publishing" line shows them all:

| Setting | Values | What it unlocks |
| --- | --- | --- |
| `RUNWAY_PUBLISHING_ENABLED` | `false` (default) / `true` | The publishing engine and the browser publisher at all. |
| `RUNWAY_PUBLISHING_MODE` | `assisted` (default) / `authorized_browser` | `authorized_browser` enables the guarded visible-Chrome publisher for YouTube Community posts. |
| `RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED` | `false` / `true` | Required with both flags above for `authorized_browser`. |
| `RUNWAY_PUBLISHING_PAUSED` | `false` / `true` | Emergency stop for every send (inbox replies, browser publisher, engine). |
| `RUNWAY_YOUTUBE_APP_STATUS` | `unverified` (default) / `verified` / `audited` | Gates publishing in hosted installs; a local owner install is always allowed. |
| `RUNWAY_INSTAGRAM_APP_STATUS` | `development` (default) / `live_standard` / `live_advanced` | `live_advanced` is what App Review + Business Verification grant; local works in `development`. |
| `RUNWAY_TIKTOK_APP_STATUS` | `sandbox` (default) / `production` / `audited` | Only `audited` ever requests `video.publish`. |
| `RUNWAY_YOUTUBE_POSTS_API_ACCESS` | `false` (default) / `true` | Set true only when Google admits the project to the posts-API pilot. |
| `RUNWAY_INBOX_REPLIES_ENABLED` | `false` (default) / `true` | Comment replies (each account still needs its engage scope). |
| `RUNWAY_YOUTUBE_ENGAGE_SCOPES` | `false` (default) / `true` | Adds `youtube.force-ssl` at connect so replies can post. |

The per-post-type capability on each account (`ready`, `ready_draft_only`,
`needs_scope`, `needs_media_host`, `needs_app_approval`, `assisted_only`) is documented
in `docs/PLATFORM_CAPABILITIES.md` and shown by the doctor.

---

## 7. Live acceptance tests (owner)

Each test settles something the research could not prove offline. Run each once, by or
with the owner, and record the date, the result and what you looked at in
`docs/VERIFICATION_REPORT.md` (the table is at `docs/expansion/IMPLEMENTATION-PLAN.md`
§"Live acceptance tests"). **[You]**

### YouTube

- **Y-1 — 7-day token trap.** With the Google project "In production" and unverified,
  connect, wait 8 days, run a sync without reconnecting. Record whether the token still
  refreshes. If it fails: reconnect weekly, or submit the app for verification.
- **Y-2 — Sync correctness.** Run a full sync of the real channel; compare ten numbers
  (subscriber count, view counts, comment counts) with YouTube Studio. Record the ten
  pairs.
- **Y-3 — The 2026-11-02 rule change.** From the new unaudited project, upload a short
  test video as `unlisted`; check it is **not** forced to private. If it fails: YouTube
  falls back to the TikTok choice (audit, vendor or manual) and the build-vs-buy
  recommendation must be revisited.
- **Y-4 — Native scheduling.** Upload as `private` with `publishAt` ten minutes ahead;
  confirm YouTube publishes it on its own. If it fails: schedule through the posting
  queue instead of `publishAt`.
- **Y-5 — Resume and reconcile.** Kill the worker mid-upload, restart it, and confirm
  exactly one video results (the attempt ledger reconciles the interrupted upload).

### Instagram

- **I-1 — Connect and sync.** Connect the real Instagram account and run a full sync.
  Record the token lifetime the token endpoint reports and any metric names the sync
  rejected.
- **I-2 — R2 signed URLs.** Give Instagram an R2 presigned URL as `image_url` for one
  image post. Record whether Instagram's fetcher accepts it. If it fails: serve the file
  from a public bucket path with an unguessable name and a short lifecycle instead.
- **I-3 — First Reel.** Publish one Reel; read `content_publishing_limit` before and
  after. Record the real remaining count — the docs disagree (100 vs 50 posts per day),
  so the endpoint is the source of truth.
- **I-4 — Large Reel.** Publish a Reel larger than 100 MB. Record whether the 5-minute
  container-status polling window is long enough; if the container expires, the code
  polls longer already — confirm it actually did.

### TikTok

- **T-1 — Token lifetimes.** Connect your TikTok in the sandbox and read `expires_in`
  and `refresh_expires_in` from the token response. Record both; never hard-code them.
- **T-2 — Inbox upload from sandbox.** Upload a video through the inbox path from the
  sandbox app and check whether a draft lands in your TikTok inbox notification.
  Passes → "send draft to my inbox" works without any audit. Fails → choose between the
  manual workflow (free, already built) and a vendor adapter (the cheapest found has two
  free connected accounts; the sign-up is yours to approve).

---

## 8. Troubleshooting

- **"Redirect URI mismatch" / `redirect_uri_mismatch` at consent.** The URI registered
  in the console must match byte-for-byte what Runway sends. `runway doctor platforms`
  prints the exact URI per platform — copy it. Watch for a `http`/`https` difference and
  a missing `/oauth/callback` suffix.
- **"unverified app" warning at Google consent.** Expected once for an unverified app
  "In production". Click through (the advanced/"continue anyway" link). It is the price
  of avoiding verification; it does not block your own account.
- **Reconnecting every ~7 days.** The OAuth consent screen is still in **Testing**
  publishing status — set it to **In production** (§2 step 4) and reconnect once.
- **Instagram publish fails with error 9007 / subcode 2207027 ("Media ID is not
  available").** A known Meta race: the error often fires seconds after the container
  reports `FINISHED`. Runway retries `media_publish` with the **same** `creation_id` for
  up to ~2 minutes; never start a second container for the same target. If it persists,
  check the container's `status_code` — `PUBLISHED` means the post went out anyway.
- **TikTok `spam_risk_too_many_pending_share`.** The inbox path allows at most **5
  pending shares per 24 hours**. Open the TikTok app and finish or discard the pending
  drafts, then retry. `spam_risk_too_many_posts` is the same idea on the direct path.
- **TikTok connect fails immediately.** TikTok requires HTTPS: the redirect URI must be
  on the public edge (§1), and `RUNWAY_PUBLIC_EDGE_URL` must be set. The doctor's TikTok
  section says which one is missing.
- **YouTube real-time goes quiet.** WebSub leases last about 5 days (432,000 s) whatever
  is requested; Runway renews at ~80% of the lease and treats each notification as a
  hint, never the record — polling still runs. Check the account's **Freshness &
  publishing** panel for the push status.
- **"this machine is in standby" in What is missing.** Connecting and storing tokens
  are writes only the machine holding the private-instance lease may do. Either connect
  on the machine that holds the lease (`runway instance status` shows it) or run
  `runway instance pull` here first.
- **Doctor shows a credential "set (settings)".** The value did not come from the
  environment, `.env`, `instance.toml` or `secrets.enc.env` — it was passed to `Settings`
  directly (tests do this). On a real install, find where it was set and move it to the
  layer you meant.
- **Edge check fails (`--check-edge`).** `https://<edge>/hooks/health` must answer `200`
  with `{"ok": true}`. A failure means the Funnel/serve mount is missing, stopped, or
  path-limited wrong — check `tailscale serve status` that `/hooks` mounts to the API.
