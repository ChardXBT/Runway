# Spec 06 — Agent interface (Muse and others)

Decision D8. Research: [`../research/agent-interface.md`](../research/agent-interface.md).

## 1. Goal

Muse keeps working exactly as it does today, and gains a documented, authenticated, scoped way
to drive the new capabilities. Any other agent can use the same interface. An agent can never
do more than its key allows, and by default cannot release a post.

Muse's own code is not in this repository, so this spec makes **no assumption** about what Muse
calls today. Rule: every existing route, the CLI and the `AgentRuntime` protocol keep their
current behaviour in the `local` profile. The new surface is additive.

## 2. Three surfaces, one authorization layer

| Surface | For | Auth |
| --- | --- | --- |
| Web UI | Humans | Session (hosted) or loopback trust (local) |
| REST `/api/v1` | Scripts, Muse, integrations | `Authorization: Bearer rwy_…` API key, or session |
| MCP server | AI agents that speak MCP | Local: stdio, launched by the agent host, key from environment. Hosted: HTTP transport with the authorization the current MCP revision requires |

All three resolve to a `Principal` (spec 01) and call the same service functions. There is no
agent-only code path that could drift from what the UI enforces.

Why both REST and MCP: REST is the stable contract and what Muse can adopt with the least
change; MCP is what general AI clients expect, and it is thin, a set of tool definitions over
the same services. Rejected: MCP only (ties the contract to a fast-moving protocol; revision
`2026-07-28` is already a breaking redesign toward stateless requests) and REST only (every
agent host would need custom glue).

## 3. API keys

- Format `rwy_<env>_<8-char prefix>_<32+ random chars>`. Store only a SHA-256 hash and the
  prefix; show the secret once at creation.
- Each key belongs to one workspace, has a name, an explicit scope list, an optional expiry, and
  can be revoked. `last_used_at` is recorded.
- Creating a key needs `workspace:admin`. A key can never be granted a scope its creator lacks.
- Default scopes offered for an "agent" key: `accounts:read`, `content:read`, `inbox:read`,
  `inbox:draft`, `media:write`, `posts:read`, `posts:write`, `posts:schedule`. **Not**
  `posts:release`, **not** `inbox:reply`, **not** `accounts:manage`.
- Per-key rate limits and a per-key daily cap on created posts.
- In `local`, requests with no key keep working as the built-in owner (invariant 17). A key is
  only needed to exercise restricted scopes, or once the owner turns on
  `RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES=true` to make Muse use a scoped key. Recommended once
  Muse has been moved to the v1 API; not forced.

## 4. Propose, do not release

The agent flow for a post:

1. Agent uploads media (`media:write`) and creates a post with targets (`posts:write`), setting
   `source='agent'`. Posts created by an agent key default `ai_generated=true`.
2. Agent may set a planned time (`posts:schedule`). The target becomes `ready`, not `released`.
3. Runway raises a `target.awaiting_release` event. The human sees it in a "Needs your approval"
   queue with the exact payload and releases or rejects it.
4. The agent learns the outcome from the event stream or a webhook.

Workspace policy `agent_release`, default `never`:

| Value | Meaning |
| --- | --- |
| `never` | No key may hold `posts:release`. |
| `per_key` | An owner may grant `posts:release` to a named key, per platform, with a daily cap. |

Even with `per_key`:

- TikTok targets can never be released by a key. TikTok requires the user to choose privacy and
  interactions by hand and to consent (research/tiktok §7). The API returns
  `403 human_required`.
- Comment replies by key need `inbox:reply`, the existing replies interlock, and remain off by
  default. A second automated responder already exists for the owner's channel (Qlob Manager);
  two would collide.

Why default `never`: an agent that reads comments and web content and can also publish is the
textbook prompt-injection target. Keeping release human is the cheapest strong defence and is
what Runway already does for Community posts.

## 5. Safety rules for agent-facing responses

- Text that came from a platform (comments, captions, titles, usernames) is returned in clearly
  typed fields and is never placed in tool descriptions, error messages meant as guidance, or
  any field the agent is told to treat as instructions.
- Tool results for untrusted text carry `"untrusted": true` on the field's wrapper object.
- Every mutation accepts an `Idempotency-Key` header; the same key with the same body returns
  the first result, with a different body returns 409. Keys are remembered for 24 hours.
- Every mutation by a key writes an audit row with the key id and name.
- Edits require the current `version` (spec 03), so an agent cannot silently overwrite a
  human's change.

## 6. REST conventions

- Base path `/api/v1`. Existing unversioned routes remain for the web app and compatibility.
- Errors are `application/problem+json` with a stable `code`, for example `scope_missing`,
  `version_conflict`, `human_required`, `capability_not_ready`, `validation_failed`.
- Lists use cursor pagination (`?cursor=…&limit=…`, response `next_cursor`).
- Times are RFC 3339 UTC.
- The OpenAPI document generated by FastAPI is the source of truth; the web app's TypeScript
  types are generated from it in CI, and a check fails when they drift.

## 7. MCP server

Package `runway.mcp`, built on the official Python SDK, exposing tools that map one-to-one to
service functions:

| Tool | Scope | Read-only |
| --- | --- | --- |
| `list_accounts`, `get_capabilities` | `accounts:read` | yes |
| `list_content`, `get_content_metrics`, `get_account_analytics` | `content:read` | yes |
| `list_comments`, `get_comment` | `inbox:read` | yes |
| `save_reply_draft` | `inbox:draft` | no |
| `upload_media_from_path` (local only), `get_media` | `media:write` | no |
| `create_post`, `update_post`, `set_target_settings`, `validate_post` | `posts:write` | no |
| `schedule_target`, `cancel_target` | `posts:schedule` | no |
| `list_posts`, `get_post`, `get_target_status` | `posts:read` | yes |
| `request_release` | `posts:write` | no; asks the human, does not release |

- No `release` tool exists in the default server. With `agent_release=per_key` a
  `release_target` tool appears only for keys that hold the scope.
- Mark tools with the protocol's read-only and destructive hints, knowing hosts treat hints as
  untrusted.
- Local: `runway mcp` runs over stdio; the agent host starts it with `RUNWAY_API_KEY` set, or
  with no key for owner trust.
- Hosted: mount the HTTP transport under `/mcp`. **Before building, read the transport and
  authorization sections of the current MCP revision**; I did not re-verify them
  (research/agent-interface §2).
- Long uploads and publishes: return the post or target id immediately and let the agent poll
  `get_target_status`. Adopt the Tasks extension only if a target host needs it.

## 8. Events for agents

Same `events` table as the browser stream (spec 04):

- `GET /api/v1/events/stream` with a key, filtered to the key's readable scopes.
- Outbound webhooks: a workspace registers a URL and a secret; Runway POSTs events signed with
  HMAC-SHA256 (`Runway-Signature: t=…,s=…`), retries with backoff for 24 hours, and disables an
  endpoint after sustained failure. For a local Muse, the stream is simpler than a webhook.

Events an agent cares about: `target.awaiting_release`, `target.released`, `target.rejected`
(with the human's reason), `target.state_changed`, `comment.new`, `proposal.decided`.

`target.rejected` and edits made by the human before release are the learning signal Muse
needs. They are exposed as structured data: what was changed, from what, to what.

## 9. Migration path for Muse

1. No change required. Everything Muse uses today still works.
2. When convenient, point Muse's new capabilities (cross-posting, video) at `/api/v1` or the MCP
   server, with a scoped key.
3. Optionally turn on `RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES` so the unauthenticated loopback API
   is read-only for everything except the legacy routes Muse still needs. The list of legacy
   routes to exempt must come from inspecting Muse (open question Q2).

## 10. Acceptance criteria

- A key without `posts:release` receives 403 `scope_missing` on release, in both profiles.
- A TikTok release by any key returns 403 `human_required`.
- Replaying a `create_post` with the same idempotency key creates one post.
- An agent edit with a stale version returns 409 and changes nothing.
- A comment containing instructions appears to the agent only inside an untrusted-typed field.
- With the MCP server running locally, an MCP client can create a post that then appears in the
  human approval queue, and cannot publish it.
- All pre-existing API tests pass with no key in `local`.
