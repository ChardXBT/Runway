# Spec 05 — Hosted platform

Decision D7 and the hosting stages. Research:
[`../research/hosting-and-sync.md`](../research/hosting-and-sync.md).

Nothing in this spec is needed for the owner's own use. It is the last phase. No service here
should be signed up for or paid for without the owner's explicit approval; each first paid step
is listed so that approval can be informed.

## 1. Components

```
Browser ──► Next.js web (server-rendered) ──► FastAPI API ──► Postgres
   │                                             │  ▲
   │  direct multipart upload                    │  └── worker (jobs: publish, sync, probe, purge)
   └──────────────► object storage (R2) ◄────────┘
Platforms ──► /hooks/* (API) ──► webhook_events ──► worker
```

- One container image, three commands: `runway serve`, `runway worker`, `runway db upgrade`.
- The web app talks to the API server-side with the user's session and client-side through
  same-origin `/api/*` rewrites, so no cross-origin setup is needed.
- The API is stateless; all state is in Postgres and object storage.

## 2. Login and workspaces

Requirement: sign-up, sign-in, password reset, email verification, workspaces with members,
roles and invitations. Building these by hand is weeks of security-sensitive work that is not
Runway's product.

| Option | Free tier | For | Against |
| --- | --- | --- | --- |
| **Clerk** | 50,000 monthly retained users; organizations included (100 active, 20 members each) [read 2026-11-14] | Organizations, roles and invitations exist out of the box and map directly to workspaces; Next.js components; FastAPI verifies a signed JWT locally | Third party holds identities; paid add-on ($100/mo) beyond 20 members per organization; lock-in |
| Better Auth (self-hosted library) [Unverified current state] | Free; data in own Postgres | No vendor; no ceiling | Runs in the Next.js server, so two services share auth tables; more to build and to secure; not verified by me |
| Auth.js, Supabase Auth, WorkOS, Auth0, Keycloak | varies | — | No built-in organizations at the free level, or heavier to run |
| Hand-rolled in FastAPI | Free | Full control | Highest risk for a solo developer; password reset, email, sessions, invitations all become Runway's problem |

**Chosen: Clerk, behind a seam.** The deciding point is that organizations with roles and
invitations are the bulk of "multi-workspace" and come free. The cost of being wrong is limited
by the seam:

- Runway's own tables are the system of record for workspaces, members and roles
  (`workspaces`, `workspace_members`). The identity provider supplies only *who the user is*
  and their organization memberships, which are mirrored into Runway's tables on sign-in and by
  the provider's webhooks.
- The API depends on one interface: `IdentityProvider.verify(request) -> ExternalIdentity`
  (verify a JWT against the provider's JWKS, cached). Swapping providers means writing a second
  implementation and migrating `users.external_id`.
- No provider SDK types appear outside `runway/identity/`.

Sessions for the API: the web app forwards the provider's short-lived session token; the API
verifies signature, issuer, audience and expiry on every request and maps the active
organization to a workspace. No session state is stored in the API.

Protection for cookie-authenticated browser calls: same-site cookies, an origin check on
state-changing requests (the existing guard's logic, with the configured web origin instead of
loopback), and a strict content security policy on the web app.

## 3. Token custody

Replaces the single encrypted file (audit C5) in hosted mode.

```
account_tokens(account_id pk, workspace_id, key_version, ciphertext, nonce,
               expires_at, refresh_expires_at, scopes_json, updated_at)
```

- Encrypt each row with AES-256-GCM (or keep Fernet for continuity; either is acceptable, pick
  one and use it everywhere). The associated data binds the row to its `account_id` and
  `workspace_id`, so a ciphertext copied to another row will not decrypt.
- The key comes from the host's secret store as `RUNWAY_TOKEN_KEYS="v2:<key>,v1:<key>"`. The
  first entry encrypts; all entries decrypt. Rotation: add a new first key, run
  `runway tokens rewrap`, remove the old key.
- Only the worker and the provider layer can decrypt. No API response model includes token
  fields. The redaction helpers in `platforms/base.py` stay in force for logs and persisted
  errors.
- `TokenStore` becomes an interface with the file implementation (local) and the database
  implementation (hosted). `local` may opt into the database implementation.
- On disconnect: revoke at the platform, then delete the row. On workspace deletion: revoke and
  delete all.

Deferred: a managed key service with per-workspace data keys. It is the stronger design and
worth doing before a security questionnaire demands it; it is not needed to start.

## 4. Platform app credentials

In hosted mode there is one Runway developer app per platform, owned by the operator. Client
ids and secrets are host secrets. Per YouTube policy there must be exactly one API project for
the product.

The capability matrix gains an **app approval** dimension per platform, set by operator
configuration and shown to users honestly:

| Setting | Effect |
| --- | --- |
| `youtube_app_status = unverified \| verified \| audited` | Unverified: connect shows the "unverified app" expectation and is capped at 100 users |
| `instagram_app_status = development \| live_standard \| live_advanced` | Below `live_advanced`, only accounts with a role on the app can connect; others see "not available yet" |
| `tiktok_app_status = sandbox \| production \| audited` | Below `audited`, direct posting is offered only as inbox draft or through another adapter |

This fixes audit C12 and prevents the product from offering an action a platform will refuse.

## 5. Compliance features that must exist before outside users

Driven by platform review requirements, not optional polish:

1. Public website with what the product does, a privacy policy and terms of service, linked
   visibly. The privacy policy names sub-processors (identity provider, storage, hosting, any
   publishing vendor) and states how Google, Meta and TikTok data is used, stored and deleted.
2. YouTube-specific disclosures: links to YouTube's Terms of Service and Google's Privacy
   Policy, and to Google's security settings page for revoking access.
3. Disconnect an account: revokes the token at the platform and deletes that account's stored
   platform data within the policy windows (7 days for YouTube Authorized Data).
4. Delete a workspace and delete a user: removes all rows, tokens and media; export first on
   request.
5. Revocation made on the platform side is detected (failed refresh, TikTok
   `authorization.removed`, Meta deauthorize callback) and triggers the same deletion path.
6. The 30-day refresh-or-delete job for YouTube data (spec 04 §3).
7. Meta data-deletion and deauthorize callback endpoints [re-read Meta's current page for the
   request format at build time].
8. An audit log a workspace owner can read: who connected, released, replied, deleted.

A lawyer should read the policies before launch. This spec is not legal advice.

## 6. Abuse and limits

A product that posts to social platforms attracts spam use, and platform approvals can be lost
because of one bad customer.

- New workspaces start with low daily release limits per platform, raised with age and history.
- Per-workspace quotas: storage, connected accounts, posts per day, API requests per minute.
- An operator kill switch per workspace and a global "pause all publishing" flag checked by the
  worker before every send.
- Sign-up may need to be invite-only at first. Recommended for the beta: it also keeps Google's
  100-user cap and Meta's tester list manageable.

## 7. Operations

- **Deploy:** container image built in CI; migrations run as a release step with the owner
  role; the app role cannot alter schema.
- **Backups:** daily Postgres dumps to object storage, 14 days kept; a restore is rehearsed
  before launch. Object storage holds only re-uploadable media, so it is not backed up.
- **Observability:** the existing JSON logs with `workspace_id`, `principal`, `job_id` added;
  health endpoints for API and worker; alerts on job failures, stuck `sending` targets,
  webhook signature failures, quota above 80%.
- **Secrets:** host secret store only; never in the image or the repository.
- **Environments:** one staging workspace with mock providers on every deploy; platform
  credentials for staging are separate developer apps.

## 8. Where to run it and what it costs

| Stage | Setup | Cost | Needs approval to spend |
| --- | --- | --- | --- |
| Beta | One small VM: API + worker + Postgres in containers, web build served by Node; R2 free tier; Clerk free tier | $0 on an existing idle or always-free VM; about $7–15/mo on Fly.io (one 1 GB machine $6.70 plus volume) | Only if Fly.io or another paid host is chosen |
| Growth | Managed Postgres, separate worker machine, paid R2 beyond 10 GB | tens of dollars per month | Yes |

Why one VM with Postgres on it for the beta, not serverless pieces: the worker must be always
on; scale-to-zero databases and sleeping free web tiers fight that (research §1); one machine is
the least to operate and the cheapest always-on shape.

Costs that scale with users: storage at $0.015 per GB-month beyond 10 GB; the 100-uploads-per-day
YouTube ceiling (needs an audit to raise, no money); a publishing vendor if used for TikTok
(research/build-vs-buy §2).

Billing is out of scope. When it comes: a merchant-of-record service spares a solo Canadian
operator from handling sales tax in many jurisdictions; compare with Stripe at that time.

## 9. Acceptance criteria

- A new user can sign up, create a workspace, invite a second user as editor, and that editor
  cannot release a post.
- Rotating the token key with `rewrap` leaves every account connected.
- Deleting a workspace leaves no rows, tokens or objects with its id (automated check).
- Restoring last night's backup into a fresh database produces a working system.
- With `tiktok_app_status=sandbox`, no UI path offers public TikTok posting.
