Job name: **rwy-tenancy** (wave 4, phase P7.1-P7.4)

Build Postgres support, enforced workspace isolation (application layer on both databases,
row-level security on Postgres) and the isolation test suite. Specs: `specs/00-principles.md`,
`specs/01-dual-use-and-tenancy.md` (all, especially §5-§7), `specs/05-hosted-platform.md` §6-§7,
`IMPLEMENTATION-PLAN.md` P7. Base: `expansion/build` at the commit named in your task (it contains
P0, P6, the hosted identity seam, media core, engine, realtime, agent and wave 3).

## Work packages

**7.1 Postgres support.**
- One set of models. Add the `psycopg` (v3, binary extra) driver pinned exactly in
  `pyproject.toml` and `requirements.lock` (a release at least 14 days old; run `pip-audit`).
- `RUNWAY_DATABASE_URL` setting (optional). When unset, today's SQLite path in the data directory
  is used unchanged (invariant 17). `postgresql+psycopg://` URLs only; reject other schemes with a
  clear error. Settings validation: `hosted` profile requires a Postgres URL; `local` accepts both.
  The URL is a secret: never logged, never in `/health`, repr-redacted.
- Make every SQLite-only construct conditional (PRAGMAs, `BEGIN IMMEDIATE`, file locks, online
  backup, `runway db downgrade` backup step - on Postgres print that the operator must take a
  `pg_dump` first and require `--i-have-a-backup`). Every Alembic revision 0013→head must upgrade
  and downgrade on Postgres (no SQLite-only SQL outside batch mode; sequences advanced after any
  explicit id insert; booleans, server defaults and partial indexes portable).
- CI: add a `backend-postgres` job to `.github/workflows/ci.yml` with a `postgres:17` service
  container (pin the image by digest if the existing workflow pins actions by SHA), running
  `alembic upgrade head`, `downgrade 0012_creator_platforms`, `upgrade head` again and the API +
  integration + unit suites with `RUNWAY_DATABASE_URL` pointing at it. Tests that are
  SQLite-specific get an explicit marker and skip on Postgres with a reason; keep that list short.
  A conftest fixture provides a fresh database per test module on Postgres (create/drop a schema
  or database) so tests stay isolated.

**7.2 Application-layer isolation (both databases).**
- `WorkspaceScoped` mixin on every model that has `workspace_id` (list them all in the report).
- A per-session workspace context (`session.info["workspace_id"]`) set once from the `Principal`
  in the request dependency and per job in the worker. `with_loader_criteria` applied in a
  `do_orm_execute` listener filters every ORM select/update/delete of a scoped model.
- A `before_flush` guard rejects inserting or moving a row into a workspace other than the
  session's. A session with no workspace context may only be used by explicitly marked system
  code (`system_session()`), never by request handlers; enforce it with a test that walks routes.
- Local profile: workspace 1 is set automatically; behaviour of every existing route unchanged.

**7.3 Row-level security (Postgres only).**
- New Alembic revision (name `00NN_row_level_security`, down_revision = your branch head; the
  orchestrator re-chains) that on Postgres enables and FORCEs RLS on every scoped table with
  policy `workspace_id = current_setting('app.workspace_id', true)::int`, and is a no-op on SQLite.
  Downgrade disables it.
- The app connects as a role that does not own the tables and has no `BYPASSRLS`; migrations run
  as the owner role (`RUNWAY_MIGRATION_DATABASE_URL`, optional, defaults to the app URL). Each
  transaction runs `SET LOCAL app.workspace_id = :id` (`set_config(..., true)`) from the session
  context. Update `deploy/compose.hosted.yml` and `docs/HOSTED_DEPLOY.md` if role setup changes.

**7.4 Isolation test suite.**
- For every scoped model: create a row in workspace A, then as workspace B assert list, get,
  update and delete cannot see or touch it (parametrised over the model list, which is computed
  from the metadata so a new scoped model is covered automatically and an unscoped table with a
  `workspace_id` column fails the test).
- Raw SQL `SELECT *` without a filter as the app role with workspace B set returns no A rows
  (Postgres CI job only).
- Inserting a row with a foreign `workspace_id` raises.
- Route walk: every `/api/v1` route with a workspace-owned resource returns 404 (not 403, not data)
  for an id from another workspace, using two hosted principals.

## Acceptance

ruff, ruff format, mypy clean; new tests fail before the change; `tests/unit` and every affected
test file pass locally on SQLite; the new Postgres CI job and the existing jobs are green on your
pushed branch. Report every scoped model, every skipped-on-Postgres test with its reason, the new
settings, and the revision id with its down_revision.
