Job name: **rwy-agent**

Job rwy-agent: build phase P5 (agent interface: keys, scopes, approval queue backend, MCP server,
outbound webhooks) of the Runway expansion.

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-agent`, branch
`expansion/k-agent` (based on `expansion/build`, which already contains phase P0 - Principal,
scopes, `get_principal` on every route, the `api_keys` table - and the shared v1 contracts:
problem+json, cursor pagination, Idempotency-Key store, `events` table, `record_event`,
`/api/v1/events/stream`). It has its own `.venv`.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly. Then read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-p0\REPORT.md` and, in
your worktree, `docs\expansion\specs\00-principles.md`, `docs\expansion\specs\01-dual-use-and-tenancy.md`
§3, `docs\expansion\specs\06-agent-interface.md` (all), `docs\expansion\research\agent-interface.md`,
and `docs\expansion\research\recheck-20261122.md` §4.

Two other jobs run at the same time: `rwy-engine` builds posts, targets, media and
`/api/v1/posts…` / `/api/v1/media…` routes and the publishing service functions; `rwy-realtime`
builds polling, webhooks inbox and `/hooks/*`. Do not build or change those. Your MCP tools for
posts and media must call service functions; since the engine's services are not on your branch,
define the MCP tool layer against a small `Protocol` of the service functions you need (create,
update, set target settings, validate, schedule, cancel, list, get, target status, request release,
upload from path, get media), register real implementations through one registry, and test the MCP
layer with a fake implementation. The orchestrator connects the real engine services after both
jobs merge; write exactly what must be connected in the report. Tools over existing data (accounts,
capabilities, content, metrics, analytics, comments, reply drafts) use the existing services
directly and are tested for real.

## Work packages (all of them)

**5.2a API keys and scopes (spec 06 §3).** Key format `rwy_<env>_<8-char prefix>_<32+ random>`;
store only a SHA-256 hash and the prefix; show the secret once; name, explicit scopes, optional
expiry, revoke, `last_used_at`. Creating needs `workspace:admin`; a key never receives a scope its
creator lacks. `get_principal` resolves `Authorization: Bearer rwy_…` in both profiles (in `local`,
no header keeps today's owner trust - invariant 17). Default agent-key scopes exactly as spec 06 §3
(no `posts:release`, `inbox:reply`, `accounts:manage`). Per-key rate limit and per-key daily cap on
created posts (settings with sane defaults). `RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES` (default false)
with an explicit, tested exemption list containing every existing pre-v1 write route (Muse's
compatibility, spec 06 §9). Routes `GET/POST /api/v1/keys`, `DELETE /api/v1/keys/{id}`. CLI
`runway keys create|list|revoke` that prints the secret once. Audit row for every mutation made by
a key (key id and name).

**5.2b Release policy and approval queue backend (spec 06 §4).** Workspace policy
`agent_release` (`never` default, `per_key` with per-platform and daily cap). TikTok release by
any key returns 403 `human_required` always. A `GET /api/v1/approvals` listing that reads targets
in `ready` created by agent keys (query through a function the engine will provide; on your branch
implement it against the existing `draft_targets` table columns that exist, guarded so it returns
an empty list when the engine's columns are absent, and document the hook). `POST
/api/v1/approvals/{target_id}/reject` with a reason recorded as a `target.rejected` event carrying
the reason and any human edits as structured before/after data.

**5.3 MCP server (spec 06 §7).** Package `runway.mcp` on the official Python SDK (`mcp`; add it
as a dependency at an exact version released at least two weeks ago that serves both MCP
revisions `2025-11-25` and `2026-07-28`; pin it in `pyproject.toml` and `requirements.lock`).
`runway mcp` runs over stdio; key from `RUNWAY_API_KEY` or owner trust with no key. Tools and
scopes exactly as the spec 06 §7 table; read-only/destructive hints; no `release_target` tool unless
`agent_release=per_key` and the key holds the scope. Untrusted platform text returned only inside
wrapper objects with `"untrusted": true` (spec 06 §5); never in tool descriptions or guidance
fields. Long operations return ids immediately. Use none of the deprecated features (sampling,
roots, logging). Tests: an in-process MCP client lists tools, calls a read tool, creates a post via
the fake service, sees it as awaiting approval, and has no way to release it; a comment containing
instructions appears only inside an untrusted wrapper.

**5.4 Outbound webhooks and agent event stream (spec 06 §8).** Table for webhook endpoints
(Alembic revision `0019_agent_interface`, down_revision = the head on your branch; the orchestrator
re-chains it; say it in the report): URL, secret (encrypted with the existing token-store key
mechanism or Fernet), event filter, state, failure counters. Delivery job: POST events signed
`Runway-Signature: t=…,s=…` (HMAC-SHA256 over `t + "." + body`), retry with backoff for 24 hours,
disable after sustained failure. `/api/v1/events/stream` filtered to the key's readable scopes.
Routes to manage endpoints (`workspace:admin`). Delivery never targets loopback or private
addresses in the `hosted` profile (SSRF guard), and in `local` only when the owner explicitly
allows it.

## Acceptance tests that must exist (spec 06 §10)

A key without `posts:release` gets 403 `scope_missing` on any release route that exists; TikTok
release by any key gets 403 `human_required`; idempotent replay of a mutation returns the first
result; different body with the same key returns 409; the MCP flow above; all pre-existing API
tests pass with no key in `local`; `RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES=true` blocks a non-exempt
write without a key and allows every exempt legacy route.

## Done when

All packages built with tests; ruff, ruff format, mypy clean; the test scope in COMMON-RULES rule 7 passes. Write `docs\AGENT_API.md`: how Muse or any agent gets a key, the v1 conventions, the
MCP setup for a desktop agent host (with a JSON config example using `runway mcp`), and the
approval flow. Report at `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-agent\REPORT.md`.
