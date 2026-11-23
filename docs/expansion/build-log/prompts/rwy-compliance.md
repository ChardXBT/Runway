Job name: **rwy-compliance** (wave 4, phases P7.5 and P8.3, P8.4)

Build the database token store, disconnect/delete/export flows with platform deauthorize and
data-deletion callbacks, the owner-readable audit log, and direct-to-storage uploads in hosted.
Specs: `specs/00-principles.md`, `specs/05-hosted-platform.md` §3 and §5, `specs/02-media-pipeline.md`
§4 and §7, `specs/04-realtime-sync.md` (deletion clock, data duties), `specs/06-agent-interface.md`
(audit rows by keys), `specs/07-private-instance-data.md` (file token store and the instance
directory). Base: `expansion/build` at the commit named in your task.

## Work packages

**7.5 Database token store with key versions.**
- `TokenStore` becomes a Protocol with the existing file implementation (local default, unchanged
  behaviour and file format) and a database implementation (`account_tokens` table: account_id pk,
  workspace_id, key_version, ciphertext, nonce, expires_at, refresh_expires_at, scopes_json,
  updated_at). New Alembic revision (`00NN_account_tokens`, down_revision = your branch head).
- AES-256-GCM via `cryptography` (already a dependency) with associated data
  `f"{workspace_id}:{account_id}:{key_version}"` so a ciphertext moved to another row fails.
- Keys from `RUNWAY_TOKEN_KEYS="v2:<base64 32 bytes>,v1:<...>"` (first encrypts, all decrypt;
  validated at startup in hosted; never logged; repr-redacted). `hosted` requires the database
  store; `local` may opt in with `RUNWAY_TOKEN_STORE=database`.
- `runway tokens rewrap` re-encrypts every row under the first key in batches inside transactions
  and reports counts (no token material). `runway tokens migrate-file-to-db` for a local owner who
  opts in (idempotent, keeps the file until `--delete-file`).
- Only the provider layer and the worker decrypt. Add a test that no API response model (walk
  the OpenAPI schema) has a field named like `*token*` with token content, and that redaction
  helpers still apply. Respect the P6 standby rule for token writes.

**8.4 Disconnect, delete, export, platform callbacks, audit view.**
- Disconnect an account: revoke at the platform through the provider (mocked in tests; YouTube
  `oauth2.googleapis.com/revoke`, TikTok `/v2/oauth/revoke/`, Instagram has no revoke endpoint -
  record that), delete its token, mark the account `disconnected`, cancel its pending jobs, and
  schedule deletion of its stored platform data (YouTube Authorized Data within 7 days; others per
  spec). The existing disconnect route keeps its contract; add `/api/v1` equivalents.
- Delete a workspace (hosted; owner role; typed confirmation) and delete a user: revoke and delete
  every token, delete every row with the workspace id across all scoped tables (compute the list
  from metadata), delete media objects through the `MediaStore`, record an audit row that survives
  in an operator log without personal data. Automated check: after deletion no row in any table
  and no stored object carries that workspace id.
- Export (`POST /api/v1/workspace/export` → job → downloadable archive of the workspace's rows as
  JSON plus a media manifest; never tokens). Local profile: `runway export` CLI writes the archive
  to a path the user names.
- Meta deauthorize and data-deletion callbacks (`/hooks/meta/deauthorize`,
  `/hooks/meta/data-deletion`): verify the `signed_request` (HMAC-SHA256 with the app secret,
  base64url, `algorithm` must be `HMAC-SHA256`), constant-time compare, trigger the same deletion
  path, and for data-deletion return `{url, confirmation_code}` with a status page route. Re-read
  the request format from Meta's documentation already captured in `docs/expansion/research/`
  (no live calls); if unclear, follow the documented `signed_request` format. Add the paths to the
  public-edge allow-list and the route-walk exemption list with a reason. A failed refresh with
  `invalid_grant`/revoked and TikTok `authorization.removed` (already handled by realtime) go
  through the same deletion path.
- Audit log view: `GET /api/v1/audit` (owner/approver; cursor pagination; who, what, when,
  principal kind, key name) over the existing `audit_log` table, recording connect, disconnect,
  release, reply, delete, key create/revoke, export. Add any missing audit writes for those
  actions. A read-only web page `/settings/audit` in `apps/web` following existing page patterns.

**8.3 Direct-to-storage uploads (hosted).**
- When the media store is `R2Store` and the profile is `hosted`, `POST /api/v1/media` returns
  `{mode: "direct", part_urls[], part_size}` with presigned part URLs that carry the exact content
  length; `complete` trusts the store's checksum for parts and still verifies the total size; the
  API never streams media bytes in hosted. Local keeps the `api` mode. Media previews in hosted use
  short-lived signed URLs, never the API origin.

## Acceptance (spec 05 §9 subset)

Rotating the token key with `rewrap` leaves every account readable; deleting a workspace leaves no
rows, tokens or objects with its id (automated check); a forged Meta `signed_request` triggers
nothing; disconnect revokes (mock asserted) then deletes; export contains no token material
(asserted by scanning the archive for every stored token value). ruff, format, mypy clean; tests
fail before the change; `tests/unit` and affected tests pass; CI green on the pushed branch.
