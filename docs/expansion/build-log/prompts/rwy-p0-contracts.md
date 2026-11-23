Job rwy-p0: STOP waiting for the heavy lock and do NOT run the full suite: COMMON-RULES rule 7 changed (re-read it); the orchestrator pushed your branch and GitHub CI runs the full suite there. Packages 0.1-0.6 are committed. Now do one more work package in the same worktree (`%USERPROFILE%\Downloads\RunWay-k-p0`,
branch `expansion/k-p0`), same rules (COMMON-RULES.md). Three jobs will start in parallel on top of
your branch next (publishing engine, real-time sync, agent interface). They all need a few shared
contracts, so build them first, small and well tested:

**0.7 Shared v1 contracts.**

1. `/api/v1` router factory that every v1 router uses (Principal dependency included, so the
   route-walk test covers it).
2. `application/problem+json` errors with a stable `code` field (`scope_missing`,
   `version_conflict`, `human_required`, `capability_not_ready`, `validation_failed`,
   `not_found`, `idempotency_conflict`, `rate_limited`) and an exception type plus handler
   (spec 06 §6). Existing unversioned routes keep their current error format.
3. Cursor pagination helper (`?cursor=…&limit=…` -> `next_cursor`, opaque cursor).
4. `If-Match` / version helper for optimistic concurrency (409 `version_conflict`).
5. Idempotency store (spec 06 §5): table `idempotency_keys(workspace_id, principal_id, key,
   request_hash, response_status, response_body, created_at)`, unique per workspace+principal+key,
   24-hour retention; same key + same body returns the stored response; different body returns 409
   `idempotency_conflict`. Provide it as a dependency/decorator v1 mutation routes opt into.
6. `events` table (spec 04 §4: `id` autoincrement, `workspace_id`, `type`, `subject_type`,
   `subject_id`, `payload_json`, `created_at`) and `record_event(session, workspace_id, type,
   subject_type, subject_id, payload)` that writes in the caller's transaction. Event types as
   string constants in one module.
7. `GET /api/v1/events/stream` (server-sent events): scoped to the principal's workspace; `id:` =
   row id; `Last-Event-ID` resume; `resync` event when the id is older than 7-day retention;
   heartbeat comment every 20 s; SQLite source polls the table every second (leave a clearly marked
   Postgres LISTEN/NOTIFY wake-up hook); payloads are ids and small summaries. Tests with a
   client that disconnects and resumes and sees every event once, in order.

One Alembic revision `0016_shared_contracts` (down_revision `0015_publish_jobs`) for the two
tables, with a tested downgrade. Update the schema contract.

Then run the rule-7 test scope, commit, and write REPORT.md covering packages 0.1-0.7 exactly as
your original prompt asked (first line the verdict), including a section "Shared contracts" giving the exact import paths and signatures of all seven items, the
event-type module, and the final migration head.
