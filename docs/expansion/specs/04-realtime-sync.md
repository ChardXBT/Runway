# Spec 04 — Real-time sync

Decision D5. Research: platform files §"Real-time"/"Webhooks",
[`../research/hosting-and-sync.md`](../research/hosting-and-sync.md) §4.

## 1. What "real-time" can honestly mean

No platform pushes everything. This table is the contract the UI must reflect; it must never
imply live data where there is none.

| Data | YouTube | Instagram | TikTok |
| --- | --- | --- | --- |
| Own post status after publishing | Poll `videos.list` (seconds to minutes) | Poll container status (once a minute) | **Push**: `post.publish.*` webhooks; poll as fallback |
| New content published elsewhere | **Push**: WebSub upload notifications | Poll | Poll |
| Title or description edits | **Push**: WebSub | Poll | Poll |
| New comments | Poll | **Push after App Review** (`comments` webhook needs Advanced Access, Live app, public account); poll until then | Not available in the Display API |
| Counts (views, likes) | Poll | Poll | Poll |
| Daily analytics | Poll; finalized after about 2 days | Poll | None; Runway's own snapshots only |
| Account disconnected | Detected on next call | Deauthorize callback [re-read at build time] | **Push**: `authorization.removed` |

So "real-time sync" in Runway means three things together:

1. **Push where offered**, received by a webhook inbox.
2. **Adaptive polling** everywhere else: fast when something just happened, slow otherwise.
3. **Live delivery to the browser** of whatever Runway learns, by server-sent events.

## 2. Webhook inbox

One public endpoint family, hosted profile (and local with a public edge, §6):

```
GET/POST /hooks/youtube/websub/{subscription_id}
GET/POST /hooks/instagram
POST     /hooks/tiktok
```

Handling, the same for all three:

1. Verify. YouTube: answer the `hub.challenge` on subscribe; if a `hub.secret` was set, check
   `X-Hub-Signature`. Instagram: `hub.verify_token` handshake; HMAC-SHA256 of the raw body with
   the app secret against `X-Hub-Signature-256`. TikTok: parse `TikTok-Signature: t=…,s=…`,
   HMAC-SHA256 of `t + "." + raw_body` with the client secret, reject timestamps more than 5
   minutes from now [Inference: TikTok gives no tolerance; 5 minutes is conventional].
   Compare with a constant-time function. Verify against the **raw** bytes before any parsing.
2. Persist the raw event in `webhook_events(id, provider, received_at, signature_ok,
   dedupe_key unique, body, processed_at, error)`.
3. Return 200 immediately. Never do work in the request.
4. A job processes the event: resolve the account, then translate to an action.

Rules:

- Webhook routes have no session and no workspace; the workspace is derived from the account
  the event names. An event for an unknown account is stored and ignored.
- A webhook is a **hint**. It triggers a targeted fetch from the platform API; Runway never
  writes platform data taken from a webhook body alone. Reason: bodies can be forged if a
  secret leaks, can arrive out of order, and are sometimes dropped.
- Deduplicate on a key derived from the provider's event identity; reprocessing must be safe.
- Body size limit 1 MB; rate limit per source.
- Keep raw events 30 days.

Subscriptions:

- YouTube: on connect, subscribe the channel's topic at the hub with a per-subscription secret.
  A `renew_subscription` job renews before the lease ends (lease length to be read from the
  hub's response; assume it can be as short as a few days).
- Instagram: after App Review, `POST /{ig-id}/subscribed_apps` on connect. Before that, do not
  subscribe; the capability matrix says "polling".
- TikTok: one callback URL per app, configured in the portal.

## 3. Adaptive polling

Replace the single fixed interval with a per-account schedule of `sync_account` jobs carrying a
scope:

| Scope | Normal | Boosted (when) |
| --- | --- | --- |
| `identity` | 24 h | on reconnect |
| `inventory` | 30 min | 2 min for 30 min after any publish or push hint |
| `content_metrics` (recent 20 items) | 30 min | 5 min for 2 h after a publish |
| `content_metrics` (older) | 24 h, rotating | — |
| `comments` (recent items) | 15 min | 2 min while the inbox page is open for that account |
| `daily_metrics` | 6 h | — |

Guards:

- **Quota budget per project.** YouTube's 10,000 units are shared by every workspace. The
  scheduler keeps a daily budget, reserves a share for publishing-related calls, divides the
  rest across active accounts, and degrades intervals rather than failing. Each account's
  budget and next sync times are visible on the Accounts page.
- **Instagram's limit scales with impressions**; read the usage headers and back off at 80%.
- **Backoff on errors** with jitter; `RateLimited` pushes the next run out, never retries in a
  loop.
- **Only sync what someone is using.** A workspace with no login for 14 days drops to daily
  syncs. This also serves YouTube's 30-day refresh-or-delete rule: a `purge` job deletes
  YouTube API data older than 30 days that has not been refreshed.

Fix first (audit C2 and C3):

- Token refresh failures that are retryable must stay retryable. Only `invalid_grant`, 401 from
  the token endpoint, or an explicit revocation marks `needs_reauth`.
- Refresh under a per-account lock (row lock on Postgres; the existing file lock locally), and
  write the new token set before releasing the lock. TikTok's rotating refresh token makes this
  mandatory.
- Never store a token lifetime constant; use `expires_in` and `refresh_expires_in` from each
  response. Warn the user 14 days before a refresh token's known end.

## 4. Event log and browser stream

```
events(id bigserial, workspace_id, type, subject_type, subject_id, payload_json, created_at)
```

Written in the same transaction as the change it describes. Types: `account.updated`,
`sync.started`, `sync.finished`, `content.imported`, `comment.new`, `target.state_changed`,
`target.progress`, `media.ready`, `quota.low`.

`GET /api/v1/events/stream` (server-sent events):

- Authenticated like any route; scoped to the principal's workspace.
- Sends events with `id:` set to the row id. On reconnect the browser sends `Last-Event-ID` and
  the server replays from there. If the id is older than retention (7 days), send a `resync`
  event and the client refetches.
- Heartbeat comment every 20 s so proxies keep the connection.
- Source: Postgres `LISTEN/NOTIFY` wakes the stream handler, which then reads new rows from the
  table; SQLite polls the table every second. The table is the truth; notify is only a wake-up.
- Payloads carry ids and small summaries, not full objects. The client refetches what it shows.

The web app replaces its polling loops (accounts sync status, draft status) with this stream and
keeps a 60-second fallback poll if the stream fails three times.

Upload and publish progress: `target.progress` events at most once per second per target, not
persisted beyond the latest value.

Why server-sent events, not WebSockets: the flow is one-way, it is ordinary HTTP, and resume is
built in. Why an events table rather than pushing straight from code: a restarted server or a
dropped connection loses nothing, and the same table feeds outbound webhooks for agents
(spec 06).

## 5. Freshness in the UI

Every number and list shows when it was last refreshed and how (`push` or `poll`). Per account
the Accounts page shows: next scheduled sync per scope, today's quota use, and which channels
are push-enabled. TikTok analytics keep the existing label that trends come from Runway's own
snapshots.

## 6. Local profile with a public edge

A loopback install cannot receive webhooks. Optional setting
`RUNWAY_PUBLIC_EDGE_URL=https://…` for a tunnel that forwards **only** `/hooks/*` and the OAuth
callback paths to the local API.

- The request guard gains an allow-list: requests whose path starts with `/hooks/` or matches
  the OAuth callback pattern are accepted from the edge host; everything else stays
  loopback-only. Webhook routes authenticate by signature; callbacks by the single-use state
  row. No session-bearing route is ever reachable through the edge.
- This also removes today's awkward TikTok setup (a host-rewriting tunnel to the whole API).
- Without the setting, local mode simply polls. Nothing breaks.

## 7. Acceptance criteria

- A forged webhook (bad signature) is stored with `signature_ok=false`, returns 200 or 401 per
  provider convention, and triggers nothing.
- Replaying the same valid webhook ten times causes one fetch.
- Closing the laptop for an hour and reopening shows every change since, in order, with no
  duplicates (stream resume test).
- With quota budget at zero, publishing-related calls still succeed and syncs are deferred with
  a visible reason.
- A transient DNS failure during token refresh leaves the account `connected`.
- Two concurrent refreshes of a rotating token result in one refresh call.
