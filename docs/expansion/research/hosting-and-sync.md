# Hosting, tenancy, jobs and real-time — facts and options

Prices read 2026-11-14 from vendor pages where a source is given. Items without a source are my
working knowledge and are marked [Unverified]; check them before choosing.

Hard constraint from the owner: spend nothing without explicit approval. So the question for
each layer is "what is the $0 option, and what is the first price step".

## 1. Verified prices

| Service | Free | First paid step | Catch | Source |
| --- | --- | --- | --- | --- |
| Cloudflare R2 | 10 GB-month, 1M Class A, 10M Class B operations per month | $0.015 per GB-month | Egress free. Signed URLs last at most 7 days and "work only with the S3 API domain, not custom domains". Signed access on a custom domain needs WAF HMAC validation on a Pro plan. | <https://developers.cloudflare.com/r2/pricing/>, <https://developers.cloudflare.com/r2/api/s3/presigned-urls/> |
| Neon Postgres | 1 GB per project, 100 compute-hours per project per month, 10 branches | $0.106 per compute-hour, $0.35 per GB-month | Scales to zero after 5 idle minutes and this "can't be turned off" on Free. | <https://neon.com/pricing> |
| Fly.io | No free tier; $1 trial credit for 7 days | shared-cpu-1x: $2.19/mo (256 MB), $3.69 (512 MB), $6.70 (1 GB) | Volumes $0.15 per GB-month; egress $0.02 per GB; stopped machines still bill storage. | <https://docs.fly.io/about/pricing> |
| Clerk | 50,000 monthly retained users; organizations included: 100 monthly retained organizations, up to 20 members each | Pro $25/mo; enhanced organizations add-on $100/mo | Third party holds user identities. | <https://clerk.com/pricing> |
| GitHub | Release assets: no total size or bandwidth limit stated | — | Files over 100 MiB are blocked in git itself. | <https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github> |

[Inference] Neon Free and an always-on job worker do not fit. A worker that polls every few
seconds keeps the database awake: 0.25 compute units × 730 hours ≈ 182 compute-hours, above the
100 included. Either the worker sleeps until the next due job, or Postgres runs on the same
machine as the app.

[Unverified] Other options not priced today: Supabase (free Postgres that pauses when
inactive), Render (free web services that sleep), Oracle Cloud Always Free (ARM VMs),
Cloudflare Tunnel (free; exposes a local service over HTTPS), Hetzner (small VPS at a few euros).

## 2. Tenancy model

| Option | For | Against |
| --- | --- | --- |
| Shared schema, `workspace_id` on every row, Postgres row-level security | One migration path; one connection pool; works on SQLite too (without RLS) so local mode shares the schema; simplest operations | A missing filter leaks data, so RLS must be a second wall and tests must prove isolation |
| Schema per workspace | Strong isolation | Alembic must migrate N schemas; connection handling per tenant; SQLite has no equivalent |
| Database per workspace | Strongest isolation; easy export and delete | N databases to migrate and back up; no cross-workspace admin queries; cost |

**Chosen: shared schema with row-level security.** The deciding factor is dual use: the same
models and migrations must run on SQLite for the local profile. Only the shared-schema option
does that. The 470 MB intelligence dataset stays where it is, in the owner's local database,
and is simply workspace 1.

SQLite to Postgres differences that will bite [working knowledge, verify during build]:

- JSON is stored as `Text` today (`*_json` columns). Keep it as text in phase 1; do not convert
  to `JSONB` in the same migration as tenancy.
- `DateTime(timezone=True)` round-trips as naive on SQLite. Runway already normalizes with
  `iso_utc`; keep doing so.
- Alembic batch mode is needed for SQLite column and constraint changes; not for Postgres.
- Case sensitivity of `LIKE` differs; sequences replace `AUTOINCREMENT`.
- SQLite has no row-level security; isolation in local mode rests on there being one workspace.

## 3. Jobs and scheduling

Requirement: "publish this at time T, once, survive restarts, never double-post, and say
'uncertain' when the outcome is unknown".

| Option | For | Against |
| --- | --- | --- |
| Own job table + worker (`SELECT … FOR UPDATE SKIP LOCKED` on Postgres; single worker on SQLite) | Runs on both databases; no new service; extends the action ledger Runway already has; full control of the uncertain state | Must be written and tested carefully |
| Procrastinate / PGQueuer [Unverified current state] | Mature Postgres queues for Python | Postgres only, so local mode would need a second mechanism |
| Celery, arq, Dramatiq, Taskiq | Familiar | Need Redis; at-least-once delivery, which is the wrong default for posting |
| Temporal, Hatchet, Inngest, DBOS | Durable workflows, good retries | A whole extra system (or vendor) for a solo operator; local mode again |

**Chosen: own job table and worker.** Publishing is not a generic background task. Its core
difficulty, the uncertain outcome after a timeout, is domain logic that a queue library does not
solve. Runway already has that logic for comment replies (`platform_actions`) and Community
posts (`publish_attempts`). One small worker that runs on both databases is less code and less
risk than adopting a queue plus writing the domain logic anyway.

## 4. Real-time to the browser

| Option | For | Against |
| --- | --- | --- |
| Server-sent events from FastAPI | One-way is all that is needed; plain HTTP; works through proxies and Cloudflare; auto-reconnect with `Last-Event-ID` | One connection per tab; needs an async endpoint |
| WebSockets | Two-way | Not needed; more to secure; the current request guard special-cases them |
| Hosted realtime (Pusher, Ably, Supabase Realtime) | No server work | Another vendor and free-tier ceiling for a simple need |

**Chosen: server-sent events backed by an `events` table.** The table is the durable log; the
stream is a tail of it. A client that reconnects resumes from its last event id, so nothing is
lost when a connection drops.

## 5. Token custody

Today: one Fernet-encrypted file, key beside it. Hosted needs:

- tokens in the database, encrypted per row, with the key held outside the database (host
  secret store);
- a key id stored with each row so keys can be rotated;
- decryption only inside the worker and provider calls, never in API responses or logs
  (Runway's redaction helpers already cover logs).

A cloud key-management service is the stronger design, but adds cost and a dependency. Start
with a host-secret key and versioned keys; move to a managed service when there are paying
workspaces. [Inference]

## 6. Where to run it

| Stage | Setup | Monthly cost | Good for |
| --- | --- | --- | --- |
| A. Local | Today's setup on the owner's machine | $0 | Owner's channels and Muse |
| B. Local with public edge | Same, plus Cloudflare Tunnel for HTTPS callbacks and webhooks, plus R2 for Instagram media | $0 [Unverified for Tunnel] | Real publishing and real-time for the owner |
| C. Hosted beta | One small VM running API, worker, Postgres and the web build; R2 for media | about $7–15 on Fly.io, or an existing idle AWS or Oracle free VM | First outside workspaces |
| D. Hosted | Managed Postgres, separate worker, backups, monitoring | grows with use | Paying customers |

Stage B is the important discovery: nearly everything in the expansion can be proven for the
owner's own accounts at $0 before any hosting exists.

## 7. Compliance baseline (not legal advice)

All three platforms' reviews require, on a public website: a description of what the app does, a
privacy policy and terms of service that are easy to find, and a working deletion or
disconnection path. YouTube additionally requires linking YouTube's Terms of Service and
Google's Privacy Policy and the Google security settings page for revocation. Meta requires
data-deletion and deauthorization callbacks [working knowledge; re-read at build time]. The
owner is in Canada (PIPEDA); users elsewhere bring GDPR and US state laws. A lawyer should
review the policies before a public launch.

## 8. Could not determine

- Current Cloudflare Tunnel, Supabase, Render and Oracle free-tier terms.
- Whether Clerk's organization model maps cleanly to Runway workspaces at the free limits over
  time (20 members per organization is enough; 100 active organizations is the ceiling).
- The current maturity of Better Auth as a self-hosted alternative.
- Real backup and restore costs at stage D.
