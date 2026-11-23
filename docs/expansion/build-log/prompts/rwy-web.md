Job name: **rwy-web**

Job rwy-web: build the web UI for the Runway expansion (phase P2.5, P4.1 client side, P5 approval
queue and keys UI, accounts freshness, connect/publishing-enable flows).

Worktree (the ONLY place you may change files): `%USERPROFILE%\Downloads\RunWay-k-web`, branch
`expansion/k-web`, based on `expansion/build`, which already contains the backend for P0, shared v1
contracts, media core, publishing engine (`/api/v1/posts`, `/api/v1/media`, cross-post bridge,
event stream), real-time sync (`/api/v1/accounts/{id}/sync-status`) and the agent interface
(`/api/v1/keys`, `/api/v1/approvals`). The YouTube and Instagram/TikTok publisher jobs run at the
same time and add a few routes (publishing enable, YouTube categories, TikTok creator-info); build
against the routes as documented in their prompts
(`%USERPROFILE%\Downloads\kiro-wave-20261122-runway\prompts\rwy-youtube.md` and `rwy-social.md`)
and handle a 404 from them gracefully. It has its own `.venv`; run `npm ci` at the repo root and in
`apps\web` first if `node_modules` is missing.

FIRST read `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\COMMON-RULES.md` and follow it
exactly (the web dev server, if you need one, must use a random free port, never 3000). Then read the
reports `rwy-p0`, `rwy-engine`, `rwy-realtime`, `rwy-agent` (`REPORT.md` in the wave folder), then in
your worktree `docs\expansion\specs\03-publishing-engine.md` §9, `docs\expansion\specs\04-realtime-sync.md`
§4-§5, `docs\expansion\specs\06-agent-interface.md` §3-§4, `docs\expansion\specs\08-platform-adapters.md`
(target settings tables and the TikTok mandatory screen), `docs\expansion\research\tiktok.md` §7, and
the existing web app under `apps\web` (its components, `lib\api.ts`, `lib\client-api.ts`,
`lib\types.ts`, tests, and design language - keep it; do not restyle the app).

## Work packages

1. **Types from OpenAPI.** Generate the TypeScript types for `/api/v1` from the FastAPI OpenAPI
   document with a script (`npm run gen:api`, using a dev dependency that is already present or
   one small well-known generator pinned exactly), commit the output, and add a check (in
   `npm run check`) that fails when the committed types drift from the backend (spec 06 §6). The
   check must run offline: export the OpenAPI JSON with the venv Python, no server.

2. **Posts.** A Posts page (`/posts`) and a post editor (`/posts/new`, `/posts/[id]`): media upload
   with chunked resumable upload and progress (spec 02 §4), per-target validation issues as soon as
   the probe finishes, caption with per-target override and live character/hashtag/mention counts,
   a settings panel per target type (fields and rules from spec 08: YouTube incl. privacy,
   made-for-kids, paid placement, category, schedule mode; Instagram types; TikTok mandatory screen
   driven by `creator-info` with no preselected privacy, interactions off by default and disabled
   when unavailable, commercial-content rules, exact consent text above the release button), time
   picker per target in the workspace time zone, `If-Match` versions with a clear "changed
   elsewhere" message on 409.

3. **Release dialog.** Shows per target the exact payload, the account, privacy in words
   ("visible to everyone who can see this account" for Instagram), AI-disclosure flag; one
   confirmation per target; echoes `payload_hash`; TikTok targets never in a batch action.

4. **Target status.** Plain-language state per target, the last attempt's result, the attempts
   ledger, and for `uncertain` a "what we checked" list with platform links and "Mark published" /
   "Check again" actions; `needs_user` explains the TikTok inbox step.

5. **Calendar.** Targets by `planned_at` on the existing Lineup calendar, with existing Lineup entries
   alongside read-only (one schedule view). Do not break the Lineup's existing behaviour or tests.

6. **"Also post to…"** on accepted proposals in the review/lineup UI, calling the cross-post route;
   nothing automatic.

7. **Approvals and keys.** "Needs your approval" queue (agent-created targets) with release and
   reject-with-reason; Settings > API keys (create with scopes, secret shown once, revoke, last
   used); outbound webhook endpoints management.

8. **Live updates and freshness.** Replace polling loops (account sync status, draft/target status)
   with the SSE stream (`EventSource` on `/api/v1/events/stream` with resume and a 60-second
   fallback poll after three failures). Accounts page shows per account: last refresh and how
   (push/poll), next sync per scope, today's quota use, push enabled, capability per post type in
   words, and an "Enable publishing" button calling `publishing/enable`.

9. Navigation entries for Posts and Approvals; nothing removed from existing nav. All compatibility
   pages (`/review`, `/lineup`, `/queue`, `/connector`, `/archive`, `/catalogue`, `/profile`,
   `/activity`, `/settings`, `/health`) keep working.

## Tests

Vitest + Testing Library for every new component and the SSE client (resume, fallback); the TikTok
screen cannot submit with no privacy chosen and shows interactions unchecked; release dialog sends
the echoed hash; 409 shows the conflict message. `npm run check` (lint, typecheck, tests, drift
check) passes at the repo root. Then build the app once (`npm --prefix apps/web run build`) to prove
it compiles. The backend suite only needs to run if you change Python (the OpenAPI export script);
if you do, run the affected tests.

## Done when

All packages built with tests; `npm run check` and the production build pass; ruff/mypy clean if
Python changed. Report at `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\rwy-web\REPORT.md` with
screenshots described in words (pages and what they show) and any backend gaps you found.
