# Spec 01 — Dual use, profiles and tenancy

Decisions D1 and D6. Research: [`../research/hosting-and-sync.md`](../research/hosting-and-sync.md)
§2, [`../research/agent-interface.md`](../research/agent-interface.md) §5.

## 1. What "dual use" means here

I read "currently Runway is integrated with Muse but I want to dual use it" as: Runway keeps
serving as the back end Muse drives for the owner's channel, **and** becomes usable on its own
by people, eventually by other creators in a hosted version. If the owner meant something else
(for example, running two channels), see `OPEN-QUESTIONS.md` Q1; the tenancy design below
covers that case too, because a second channel is a second workspace.

## 2. Profiles

One setting, `RUNWAY_PROFILE`, default `local`.

| | `local` | `hosted` |
| --- | --- | --- |
| Bind address | loopback only (today's validator stays) | any; behind a reverse proxy |
| Request guard | `LocalRequestGuard` unchanged | replaced by session and key authentication plus standard CSRF and CORS for the web origin |
| Login | none | required |
| Workspaces | exactly one, id 1, created by migration | many |
| Principal | built-in `local-owner` with every scope | from session or API key |
| Database | SQLite | Postgres |
| Media | local disk | object storage |
| Token store | encrypted file (today) or database | database, per-row encryption |
| Intelligence engine, browser publisher, capture | available | **disabled** (routes return 404; nav hides them) |
| Agent keys | optional | supported |

Why profiles and not a fork: one codebase means the owner's install receives every fix, and the
hosted product is exercised daily by the owner's own use of the shared parts. See
research/agent-interface §5 for the alternatives.

Why the intelligence engine is local-only: it is single-tenant by design (NumPy brute-force
retrieval over one 470 MB database, local model CLIs, a browser profile). Making it multi-tenant
is a separate project with no demand yet. Hosted workspaces get connection, sync, inbox,
analytics, drafts, scheduling and publishing.

Startup validation (fail fast, in `Settings`):

- `hosted` with a SQLite URL: refuse.
- `hosted` without an identity provider configured: refuse.
- `hosted` with `publishing_mode=authorized_browser`: refuse.
- `local` with a non-loopback host: refuse (unchanged).

## 3. Principal

```python
class Principal(BaseModel):
    kind: Literal["user", "agent", "system"]
    id: str                 # user id, api key id, or "worker"
    workspace_id: int
    role: Literal["owner", "editor", "approver", "viewer"] | None   # users only
    scopes: frozenset[str]  # effective scopes after role or key expansion
    display: str            # for audit rows
```

- A FastAPI dependency `get_principal` is required by **every** route in both profiles.
- In `local` it returns the built-in owner for workspace 1 (unless an API key header is present,
  in which case the key is resolved normally, so agent scopes can be exercised locally).
- Authorization is one function, `require(principal, scope)`. No route checks roles directly.
- A lint-style test walks the router and fails if any route lacks the dependency.

Scopes (shared by users, keys and MCP):

| Scope | Allows |
| --- | --- |
| `accounts:read` / `accounts:manage` | See / connect, disconnect, resync accounts |
| `content:read` | Library, analytics, sync status |
| `inbox:read` / `inbox:draft` / `inbox:reply` | Read comments / save reply drafts / send a reply |
| `media:write` | Upload media |
| `posts:read` / `posts:write` | Read / create and edit posts and targets |
| `posts:schedule` | Set or change the planned time |
| `posts:release` | Release a target to its platform |
| `workspace:admin` | Members, keys, policies, deletion |

Roles expand to scopes: viewer = the `:read` scopes; editor = viewer + `inbox:draft`,
`media:write`, `posts:write`, `posts:schedule`; approver = editor + `posts:release`,
`inbox:reply`; owner = everything.

## 4. Data model changes

New tables:

```
workspaces(id, slug unique, name, profile_note, created_at, deleted_at)
users(id, external_id unique, email, display_name, created_at)            -- hosted only rows
workspace_members(workspace_id, user_id, role, created_at)  unique(workspace_id, user_id)
api_keys(id, workspace_id, name, prefix, key_hash, scopes_json, created_by,
         created_at, last_used_at, expires_at, revoked_at)
audit_log(id, workspace_id, actor_kind, actor_id, action, target_type, target_id,
          detail_json, created_at)     -- may reuse the existing audit_events with new columns
```

Changes to existing tables:

- Add `workspace_id INTEGER NOT NULL` (foreign key to `workspaces`) to every platform table:
  `connected_accounts`, `oauth_states`, `content_groups`, `platform_content`,
  `content_metric_snapshots`, `account_metric_snapshots`, `account_metric_days`,
  `comment_records`, `reply_drafts`, `platform_actions`, `sync_runs`, `sync_cursors`,
  `content_drafts`, `draft_targets`, and every new table in specs 02–04.
- Add it also to `channels` (the root of the intelligence tables). The other ~50 intelligence
  tables hang off `channel_id` and are local-only; they do **not** get the column in phase 1.
  Reason: it would be a large migration of a 470 MB production database for no functional gain
  while those features are disabled in hosted mode.
- Change `uq_connected_account_external` from `(platform, external_account_id)` to
  `(workspace_id, platform, external_account_id)`.
- Denormalize `workspace_id` onto child rows instead of relying on joins. Reason: row-level
  security policies and every query filter then need no join, and a bug cannot leak through a
  forgotten join.

Decision to record for the product: **may two workspaces connect the same channel?** Specified
answer: yes. The unique constraint is per workspace. A platform account that is connected in two
workspaces gets two independent token sets. This avoids one workspace locking another out of an
account, at the cost of duplicate sync. Revisit if abuse appears.

## 5. Isolation

Layer 1, application: a `WorkspaceSession` wrapper sets the workspace once per request and every
repository query filters on it. Use SQLAlchemy's `with_loader_criteria` on a `WorkspaceScoped`
mixin so the filter is applied automatically to every select of a scoped model, and add a
`before_flush` hook that rejects inserts whose `workspace_id` differs from the session's.

Layer 2, database (Postgres only): enable row-level security on every scoped table with the
policy `workspace_id = current_setting('app.workspace_id')::int`. Set it with
`SET LOCAL app.workspace_id = :id` at the start of each transaction. The application connects as
a role that does not own the tables and has no `BYPASSRLS`; migrations run as a separate owner
role. The worker sets the workspace per job.

Why both: layer 1 gives correct behaviour and good errors on both databases; layer 2 means a
missed filter in new code returns nothing instead of another workspace's data.

Tests that must exist before any hosted deployment:

1. For every scoped model: create a row in workspace A, query as workspace B, assert not found,
   for list, get, update and delete.
2. A raw SQL select without a filter, as the application role with workspace B set, returns no
   A rows (Postgres job in CI).
3. Inserting a row with a foreign `workspace_id` raises.
4. The route-walk test from §3.

## 6. Migration plan

Preconditions (from audit findings C7): remove import-time app creation side effects.
`apps/api/main.py` keeps `app = create_app()` for the launcher, but `create_app` must not run
migrations. Migrations run only through an explicit `runway db upgrade` command, which the
launcher script calls. Reason: the tenancy migration is large and must never be applied to the
production database by a stray import.

Steps, each its own Alembic revision, each rehearsed on a copy of production first (the
procedure in `docs/VERIFICATION_REPORT.md` "Database migration rehearsal"):

1. `0013_workspaces`: create `workspaces`, `users`, `workspace_members`, `api_keys`; insert
   workspace 1 named after `RUNWAY_CHANNEL_NAME`.
2. `0014_workspace_columns`: add nullable `workspace_id` to the scoped tables; backfill 1; make
   it non-null; replace the unique constraint. Use Alembic batch mode for SQLite.
3. `0015_rls` (Postgres only, no-op on SQLite): enable policies.

Each revision has a tested downgrade. Row counts before and after must match for every existing
table.

Postgres support: keep one set of models. Run the full test suite against both SQLite and a
Postgres service container in CI. Do not convert `*_json` text columns to `JSONB` in this work.

## 7. Acceptance criteria

- `local`: every existing test passes unchanged; no login prompt appears; the Supervisor health
  check and `/review` behave as before; production data rehearsal shows identical row counts.
- `hosted`: an unauthenticated request to any `/api` route returns 401; a member of workspace A
  cannot read or alter anything in workspace B (tests in §5); intelligence, capture, connector
  and browser-publisher routes return 404.
- CI runs the API suite in both profiles and on both databases.
