# Hosted Runway: deployment and operations

This covers the hosted profile (`RUNWAY_PROFILE=hosted`, spec 05). None of it is
needed to run Runway on your own machine: the local profile keeps working with no
login, no cloud account and no paid service (spec 00, invariant 17).

Nothing here should be signed up for, bought or switched on without the owner's
explicit approval. Each step that needs an account says so.

## 1. Status: what exists and what is still missing

Built (branch `expansion/s-hosted`):

- Sign-in through Clerk, verified by the API on every request (`runway.identity`).
- Workspaces mirrored from Clerk organizations, with Runway's own roles
  (`workspace_members`), a webhook receiver, and `/api/v1/workspace`.
- Hosted hardening: web-origin CORS and origin checks, rate limits, quotas,
  the publishing kill switches, security headers, local-only routes hidden.
- The container image, a compose file, backup and restore scripts, a CI image build.

Still required before a hosted deployment can start (other work packages):

| Missing | Why it blocks | Where it is tracked |
| --- | --- | --- |
| Postgres support: a driver (`psycopg`), `RUNWAY_DATABASE_URL`, Postgres-aware `runway db` | `Settings` refuses `hosted` on SQLite; the compose file already passes `RUNWAY_DATABASE_URL` | spec 01 section 6 |
| `runway worker` | the `worker` service runs it | job queue work package (phase P0) |
| Row-level security policies | the app role is created without `BYPASSRLS` so the policies will bind it | spec 01 section 5 |
| Database token store, `RUNWAY_TOKEN_KEYS` | hosted tokens must not live in a file | spec 05 section 3 |
| Clerk in the web app (sign-in pages, organization switcher, forwarding the session token) | the API accepts Clerk tokens; the web app does not obtain them yet | web work package |
| API keys (`rwy_` bearers) | refused with `api_key_unsupported` until built | spec 06 |
| Workspace scoping and scope checks on the platform routes (accounts, connect/disconnect, content, inbox, drafts, overview, analytics) | their queries read and write the global tables with no `workspace_id` filter and call no `require()`, so one workspace's member could read or disconnect another's accounts. Hosted therefore does **not mount** that router (404) | workspace tenancy (wave 4, spec 01 sections 4-5): add `workspace_id` filters and `require(ACCOUNTS_MANAGE / INBOX_REPLY / POSTS_WRITE ...)`, then mount it in hosted |
| `enforce_quota` call sites for storage (media upload) and posts per day (the publishing engine) | the quotas exist; connected accounts is enforced on connect, posts per day needs the engine's daily count, storage needs the upload path | media pipeline (spec 02) and publishing engine (spec 03) |

## 2. The pieces

| Piece | What it is |
| --- | --- |
| `Dockerfile` | `python:3.13-slim`, `ffmpeg` installed, runs as the non-root user `runway` (uid 10001). Installs `requirements.lock` exactly, then Runway itself. Entry point `runway`; commands `serve` (default), `worker`, `db upgrade`, `hosted ...`. No secret is baked in. |
| `deploy/web.Dockerfile` | The Next.js web app. `NEXT_PUBLIC_RUNWAY_API_URL` is a build argument (public by design). Telemetry disabled. |
| `deploy/compose.hosted.yml` | One machine: `postgres`, `migrate` (release step), `api`, `worker`, `web`. Every secret is an environment placeholder; compose refuses to start when a required one is missing. Postgres has no published port; `api` and `web` publish on loopback only, for the reverse proxy. |
| `deploy/postgres/init-roles.sh` | Runs once on a new Postgres volume. Creates the **owner** role (owns the schema; migrations and restores connect as it) and the **app** role (API and worker; row access only, `NOBYPASSRLS`, cannot alter the schema). |
| `deploy/backup.sh`, `deploy/restore.sh` | Daily `pg_dump` to object storage through rclone or the AWS CLI, 14 days kept; restore into an empty database only. |
| `.github/workflows/ci.yml`, job `container-image` | Builds both images on every push, checks the user is not root, `ffprobe` and the CLI run, and the compose file parses. Pushes nothing. |
| `runway serve` | In hosted it binds `RUNWAY_HOST` (the image sets `0.0.0.0`) and trusts `X-Forwarded-*` only from `RUNWAY_FORWARDED_ALLOW_IPS`. In local it still refuses anything but loopback. |

Request path in the API (hosted): security headers, per-IP rate limit (requests
without a credential; see section 4), origin guard, CORS, then `get_principal`:
verify the Clerk session, per-user rate limit, mirror the user, resolve the
workspace and the Runway role, per-workspace rate limit. Routes call
`require(principal, scope)`.

Routes served in hosted today: `/health`, `/api/settings` (product switches only),
`/api/v1/workspace*` (always filtered by `principal.workspace_id`) and the signed
webhook `/hooks/identity/clerk`. Everything else is 404 until it is workspace-scoped.

## 3. What the owner must sign up for and approve

| Step | Account or approval | Cost at this stage | First paid step |
| --- | --- | --- | --- |
| Clerk application | Clerk account, one application (Hobby plan) | $0 up to 50,000 monthly retained users, 100 organizations, 20 members each (read 2026-11-22) | Pro $25/month; Enhanced B2B add-on $100/month (not needed: Runway keeps its own roles) |
| A machine to run the containers | An existing idle VM, or a paid host | $0 on an existing VM; about $7-15/month on Fly.io | Any paid host |
| A domain with TLS | DNS for two names, for example `app.` and `api.` | Existing domain: $0; certificates from the proxy (Caddy or a Cloudflare Tunnel) | A new domain |
| Object storage for backups | A Cloudflare R2 bucket (or any S3-compatible bucket) | 10 GB-month free on R2 | $0.015 per GB-month beyond |
| Platform developer apps | Google, Meta and TikTok apps owned by the operator | $0; reviews take time | none in money |

### Clerk setup (owner, in the Clerk dashboard)

1. Create an application. Enable **Organizations**. Keep the default roles
   (`org:admin`, `org:member`): Runway's four roles live in Runway, not Clerk.
2. Note the **Frontend API URL**. It is the token issuer: set
   `RUNWAY_CLERK_ISSUER` to it. The JWKS URL is `<issuer>/.well-known/jwks.json`;
   set `RUNWAY_CLERK_JWKS_URL` only if Clerk shows a different one.
3. Optional but recommended: customise the session token with
   `{"email": "{{user.primary_email_address}}", "name": "{{user.full_name}}"}`
   so members show a name before the first `user.*` webhook arrives.
4. **Webhooks**: add the endpoint `https://<api origin>/hooks/identity/clerk`,
   subscribed to `user.created`, `user.updated`, `user.deleted`,
   `organization.created`, `organization.updated`, `organization.deleted`,
   `organizationMembership.created`, `organizationMembership.updated`,
   `organizationMembership.deleted`. Copy its signing secret (`whsec_...`) into
   the host secret store as `RUNWAY_CLERK_WEBHOOK_SECRET`. To rotate, put the new
   and old secrets in the variable separated by a space, then remove the old one.
5. **Origins**: Clerk puts the origin of the page that requested a session token
   in its `azp` claim. Set `RUNWAY_WEB_ORIGINS` to the web app's origin; it is the
   accepted `azp` value unless `RUNWAY_CLERK_AUTHORIZED_PARTIES` lists others.
6. Invite-only beta (spec 05 section 6): use Clerk's sign-up restrictions (for
   example an allowlist) so only invited people can create accounts.

The claim names below were written from working knowledge, not re-read for this
build; check them against Clerk's current session-token reference during setup.

### How Clerk maps to Runway

- Each Clerk organization is one Runway workspace, created the first time it is seen.
- The first organization **admin** to arrive (by sign-in or by webhook) becomes the
  workspace `owner`. Everyone after joins as `RUNWAY_HOSTED_DEFAULT_MEMBER_ROLE`
  (default `viewer`). An owner changes roles with
  `PATCH /api/v1/workspace/members/{user_id}`; the last owner cannot be demoted.
- **Invitations and removals are done in Clerk** (its `<OrganizationProfile />`
  component or the dashboard). Runway mirrors them through the webhook. Until the
  membership webhook arrives, a newly invited user gets `403 membership_unknown`.
- Removing a member, deleting a user or deleting an organization revokes access.
  It never deletes workspace data; workspace deletion is a separate flow.
- Session token claims: Clerk's v2 tokens carry the active organization as
  `o.id`/`o.rol`; v1 tokens as `org_id`/`org_role`. Both are read; `o.id` wins.

## 4. Configuration

Required in hosted (startup refuses without them):
`RUNWAY_IDENTITY_PROVIDER=clerk`, `RUNWAY_CLERK_ISSUER` (https),
`RUNWAY_CLERK_WEBHOOK_SECRET`, `RUNWAY_WEB_ORIGINS` (https origins; plain http only
for a loopback rehearsal), and a Postgres database.

Limits and switches (defaults in brackets): `RUNWAY_RATE_LIMIT_IP_PER_MINUTE` [300],
`RUNWAY_RATE_LIMIT_PRINCIPAL_PER_MINUTE` [120], `RUNWAY_QUOTA_STORAGE_BYTES` [1 GiB],
`RUNWAY_QUOTA_CONNECTED_ACCOUNTS` [3], `RUNWAY_QUOTA_POSTS_PER_DAY` [10],
`RUNWAY_QUOTA_API_REQUESTS_PER_MINUTE` [120], `RUNWAY_PUBLISHING_PAUSED` [false].
The rate limiter is in-process: with more than one API process, each keeps its own
buckets until a shared store implements `RateLimiter`.

Secrets live in the host's secret store or an untracked env file passed with
`--env-file`. Never in the image, the repository or a CI log.

## 5. Deploying

```sh
# on the host, with /etc/runway/hosted.env readable only by the operator
compose="docker compose -f deploy/compose.hosted.yml --env-file /etc/runway/hosted.env"
$compose build api web
$compose up -d postgres
$compose run --rm migrate          # release step: migrations as the owner role
$compose up -d api worker web
```

The reverse proxy terminates TLS for the web and API origins and forwards to
`127.0.0.1:${RUNWAY_WEB_HOST_PORT:-3100}` and `127.0.0.1:${RUNWAY_API_HOST_PORT:-8080}`.

## 6. Operating

| Task | How |
| --- | --- |
| Pause all publishing | set `RUNWAY_PUBLISHING_PAUSED=true` and restart `api` and `worker`. Every platform-mutating send calls `runway.hosted.quotas.require_send_allowed` first (inbox replies, the browser publisher, and the publishing engine once it lands) and is refused with `409 publishing_paused`; nothing is recorded as sent |
| Stop one workspace from publishing | `runway hosted pause-publishing <workspace id> --reason "..."`; undo with `resume-publishing` |
| Raise or lower a workspace's quotas | `runway hosted quota <id> --posts-per-day 25` (`--clear` restores the defaults) |
| See a workspace's limits | `runway hosted status <id>` |
| Watch for | log events `identity_webhook_signature_failed`, `identity_jwks_refresh_failed`, `identity_last_owner_removed`, `quota_near_limit` |

Hosted serves 404 for the local-only surfaces: capture, catalogue, discovery,
intelligence, the proposal and Lineup flow, the browser publisher and its
connector, `/media` and the generated API docs; and, until they are scoped to a
workspace, the platform routes (accounts, content, inbox, drafts, overview). `/api/settings` shows tenants
product switches only.

## 7. Backups

`deploy/backup.sh` once a day (cron or a systemd timer), connected as the owner role:

```sh
PGHOST=... PGDATABASE=runway PGUSER=runway_owner PGPASSWORD=... \
RUNWAY_BACKUP_TOOL=rclone RUNWAY_BACKUP_REMOTE=r2:runway-backups/postgres \
deploy/backup.sh
```

It writes a custom-format dump plus a SHA-256 file, checks the dump is readable,
uploads both and deletes objects older than 14 days by the timestamp in their name.
rclone or the AWS CLI reads its own credentials; the script holds none. Media in
object storage is re-uploadable and is not backed up.

### Rehearsed restore (before launch, then every quarter)

1. Create a **fresh** Postgres (a new compose project or a scratch server) and run
   `deploy/postgres/init-roles.sh` against it, so the owner and app roles exist.
2. As the owner role: `RUNWAY_RESTORE_CONFIRM=<db name> deploy/restore.sh latest`.
   It refuses a database that already has tables, verifies the checksum, restores in
   one transaction and prints the migration revision.
3. `runway db current` against it reports `up_to_date: true` for the image you deploy.
4. Start `api` against the restored database and sign in: your workspace, its
   members and their roles are as they were.
5. Connected accounts still sync (token store), and a draft can be scheduled.
6. Row counts of `workspaces`, `workspace_members`, `connected_accounts` and
   `content_drafts` match the production database at the backup time.
7. Write down the date, the backup file name, how long the restore took and any
   problem, in the operations log.
