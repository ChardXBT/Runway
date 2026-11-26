# Runway expansion build: handoff (2026-11-22, 19:50 EST)

Read this file first. It holds everything the previous orchestrator (Claude, Opus 5.5) knew when
it stopped: the goal, decisions, the state of every branch, what is verified and what is not, the
review findings, mistakes made, how the background jobs were run, and the ordered list of what is
left. The specs it builds from are in this folder (`README.md`, `specs/00-09`,
`IMPLEMENTATION-PLAN.md`, `OPEN-QUESTIONS.md`, `research/`, especially
`research/recheck-20261122.md`). Raw job prompts, rules, reports and scripts are in
[`build-log/`](build-log/).

Paths below use `%USERPROFILE%` (the owner's Windows profile). All work lives under
`%USERPROFILE%\Downloads`.


## 0. Integrated verification update (2026-11-26)

This handoff's branch table and older verification counts are historical. The integrated
`expansion/build` branch at `4f25721` has now been re-verified as a whole: backend 2061 passed /
31 skipped before the review fix and 2062 passed / 31 skipped after it; frontend 46 files / 228
tests plus lint, typecheck, API drift and production build passed; upstream GitHub CI and Secret
scan were green on `4f25721`. A synthetic SQLite copy was migrated from `0012_creator_platforms`
to `0020_youtube_publishing` and back through the public CLI: both commands exited 0, integrity
checks were clean, foreign-key checks were empty, scoped fixture rows were preserved at head, and
the downgraded schema fingerprint matched the expected 0012 fingerprint. Exact evidence is in
`docs/VERIFICATION_REPORT.md` under "Integrated-branch re-verification (2026-11-26)".

The same review found and fixed one OAuth workspace-binding defect in the legacy connect helper.
A second independent review then found that fix was incomplete: callbacks did not enforce the
recorded workspace and Unified Login states were still unbound. The PR now makes OAuth state
consume/peek workspace-aware, binds Unified Login connect/select states to the caller, and proves
cross-workspace callbacks/picks cannot consume or use another workspace's state. Final counts are in
`docs/VERIFICATION_REPORT.md` (backend `2065 passed, 31 skipped`; frontend `46 files / 228 tests`).

Legacy `/api/platforms` routes remain a local single-workspace surface and are excluded from the
hosted allowlist; do not expose them in hosted mode without adding workspace filters throughout.

## 1. Goal and the owner's words

- 2026-11-22: "Complete the runaway repo do deeper research you have full permission Runway should
  be done" (the expansion specs 00-09, unbuilt until today).
- "Runway [Expansion]: Connect real YouTube, Instagram and TikTok accounts. Add video publishing,
  real-time sync and hosted multi-workspace support."
- "Want this thing production ready fully finished then I will connect my accounts. Continue."
- "Yep you can run sub agents in this chat too. Continue I want the best solution."
- "Great continue you have full permission."

**Finish line:** every phase P0-P8 of `IMPLEMENTATION-PLAN.md` built, independently reviewed,
tested (GitHub CI full suite), merged to `main`, and deployed to the owner's live local Runway, with
a tested connect flow and a runbook (`docs/CONNECT_ACCOUNTS.md`) so the owner can create the
developer apps and connect his accounts himself. Nothing hosted is deployed or signed up for.

**Never:** spend money, sign up for services, create developer apps, connect real accounts, make
real platform calls, publish anything, touch the production database except through the deploy
procedure in §9, commit secrets or production data to this PUBLIC repository.

## 2. Decisions already taken (do not re-litigate)

Recorded in `OPEN-QUESTIONS.md` "Decisions taken for the build":

- One codebase, `local` and `hosted` profiles (`RUNWAY_PROFILE`); every row has `workspace_id`.
- Muse: its code and request logs are not on this machine, so every existing route stays unchanged;
  `/api/v1` is additive; `RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES` defaults to false.
- TikTok: build `tiktok_inbox` (draft to the creator's inbox; default once connected) and
  `assisted`; `tiktok_direct` built against mocks but hidden below `tiktok_app_status=audited`. No
  vendor adapter (needs an owner sign-up).
- YouTube Community posts: new `youtube_post` adapter for the `posts` API pilot, gated by
  `youtube_posts_api_access` (default false). Browser publisher stays the default.
- Hosted: built and tested with mocks only; Clerk behind an `IdentityProvider` seam; Runway's own
  `workspace_members` holds Runway roles (Clerk free tier has only admin/member).
- Private instance data: private companion repo + SOPS/age (spec 07), never git-crypt in this repo.
- Public edge for TikTok callback/webhooks: Tailscale Funnel (installed and signed in on the
  laptop) exposing only `/hooks/*` and OAuth callback paths; the owner turns it on.
- Test strategy: builders run targeted tests + `pytest tests/unit` locally; GitHub CI runs the full
  suite (~10 min there vs 32-40 min on the laptop, which also runs out of RAM).
- Model rule: Opus 5.5, high effort for code; never Fable.

## 3. Repository layout

| Worktree (`%USERPROFILE%\Downloads\...`) | Branch | Head | Purpose |
| --- | --- | --- | --- |
| `RunWay` | `main` (local 5b3bad8, 10 behind origin) | - | **LIVE production checkout. Do not edit.** Deploy target (§9). |
| `RunWay-expansion-research` | `research/expansion-20261113` | 07c6ee0 | old research branch (already merged into build) |
| `RunWay-expansion-build` | `expansion/build` | this commit | **integration branch**: origin/main 8a84a46 + research + recheck + media core + this handoff |
| `RunWay-k-p0` | `expansion/k-p0` | 08cf900 (WIP on top of fb71830) | P0 foundations + 0.7 shared contracts + most review fixes |
| `RunWay-k-p6` | `expansion/k-p6` | 82c8939 | P6 private instance data + all review fixes (unreported) |
| `RunWay-k-media` | `expansion/k-media` | e9c3514 | media core; **merged** into build at 059d636 |
| `RunWay-s-hosted` | `expansion/s-hosted` | b954366 | P8 hosted identity seam, hardening, Docker/compose/backups |
| `RunWay-k-engine` | `expansion/k-engine` | 1396248 (WIP) | P2 publishing engine, partial |
| `RunWay-k-agent` | `expansion/k-agent` | d71b40c (WIP) | P5 agent interface, partial |
| `RunWay-k-realtime` | `expansion/k-realtime` | 404529e (nothing built) | P4 real-time, not started |

Every worktree has its own `.venv` (Python 3.13; `pip install -r requirements.lock` then
`pip install --no-deps -e .`; about 4 minutes; create venvs one at a time - a parallel batch failed
once). All branches are pushed to `origin` (ChardXBT/Runway, public).

### Migration chain

```
0012_creator_platforms (production today)
 └ 0013_workspaces ─ 0014_workspace_columns ─ 0015_publish_jobs ─ 0016_shared_contracts   (k-p0)
      ├ 0016h_hosted_identity (s-hosted; down_revision 0013 → re-chain after 0016 or the newest head)
      ├ 0017_publishing_engine (k-engine WIP; down 0016)
      ├ 0018_webhook_inbox (realtime; planned, down 0016 → re-chain after 0017)
      └ 0019_agent_interface (k-agent WIP; down 0016 → re-chain after 0018)
```

When merging, re-chain each revision's `down_revision` so the history is linear, regenerate
`docs/schema/intelligence-data-flywheel.json` (schema contract) and fix tests that assert a head
(they should compute it from Alembic's `ScriptDirectory`; P0 fix item 5). `s-hosted` carries a
verbatim copy of 0013 in commit 0a8fb83: drop that commit (or take k-p0's file) when merging.

## 4. State of every piece

### Merged into `expansion/build`
- Research re-check (`research/recheck-20261122.md`), spec updates (08 §2 `youtube_post` and
  paid-placement flag, 08 §3 9007 rule and field rules, 04 §2 WebSub lease and IG token upkeep,
  06 §7 MCP 2026-07-28), decisions in OPEN-QUESTIONS.
- **Media core** (`src/runway/mediastore/`, `src/runway/platforms/limits.py`): storage port,
  resumable `LocalDiskStore`, `R2Store` with its own SigV4 signer (AWS test vectors), locked-down
  ffprobe probe with demuxer whitelist, MP4 atom reader, per-platform validation, YouTube/TikTok/
  Instagram delivery helpers, plus 11 review fixes. CI green (run 45067601092); real-ffmpeg
  playlist tests re-run by the orchestrator (48 passed). Fixes were not re-reviewed.

### `expansion/k-p0` (P0) - nearly done
- Built and CI-green at 404529e (run 45066533417): 0.1 no migrations at import + `runway db
  upgrade/current` + stale-DB refusal + launcher upgrade; 0.2 profiles, `Principal`, `require()`,
  `get_principal` on every route, route-walk test; 0.3 refresh fixes and per-account lock; 0.4
  state machines; 0.5 migrations 0013/0014; 0.6 `publish_jobs`, queue, `runway worker`, sync on
  jobs, launcher starts the worker; 0.7 shared v1 contracts (router factory, problem+json, cursor
  pagination, If-Match, idempotency store, events table + `record_event`, SSE stream with resume).
  Interfaces: `build-log/reports/rwy-p0-REPORT-2.md` "Shared contracts".
- Review fixes (brief `build-log/prompts/rwy-p0-fix1.md`), committed after 404529e:
  56eb323 items 1+4 (transactional SQLite DDL, crash-safe and Postgres-safe 0013-0016),
  a24c30a items 6+7, 0f84b37 item 10, 74a8b10 item 8, fb71830 item 9.
- **WIP 08cf900 (unverified, ruff clean):** items 2+3 in progress - `db/backup.py`,
  `operations/revision.py`, `operations/runtime_files.py` (worker pid), `operations/console_stop.py`,
  `cli/db_commands.py` (downgrade), worker command changes. Still to do: finish items 2, 3, 5, 11
  (launcher must never stop a healthy API for a worker failure; worker recognised by lock/pid;
  `/health` gains the revision; launcher upgrades a behind DB and restarts an older API;
  `runway db downgrade` with backup; OPERATIONS.md deploy/rollback order; literal-head tests;
  flaky/unfailable tests), then write the final REPORT.

### `expansion/k-p6` (P6) - built, needs verification
- Built: instance dir and load order, `runway instance init/status/push/pull/handover`, lease and
  standby, manifest + SQLite online backup snapshots, `-Repository` on the release script, secret
  scan in pre-commit and CI, owner guide `docs/PRIVATE_INSTANCE.md`. CI green before the fixes.
- Review fixes (brief `build-log/prompts/rwy-p6-fix1.md`) appear committed: 2738e72 (items 1, 5),
  b159550 (4, 8, 9), 6c7a1ba (6), 1ba1502 (2, 3), 82c8939 (docs). Items 7 and 10 not obviously in a
  commit subject - check. **No final REPORT was written** (the job ended at session end). CI on
  82c8939 was triggered by the final push; check it.

### `expansion/s-hosted` (P8 seam) - built
- `runway.identity` (Clerk RS256 JWT verification with `cryptography`, JWKS cache, v1 `org_*` and v2
  `o` claims - verified against Clerk's docs), hosted `get_principal`, Clerk (Svix) webhook mirror,
  `/api/v1/workspace`, rate limiter, quotas, kill switches, `publishing_allowed()`, hosted hardening
  (origin checks, security headers, local-only routes 404), Dockerfile, `deploy/compose.hosted.yml`
  (owner and app DB roles), backup/restore scripts, `docs/HOSTED_DEPLOY.md`, CI image build job.
  Unit suite 424 passed, ruff/mypy clean (subagent's report). Not independently reviewed.
- Hosted cannot start yet: needs Postgres support (driver, `RUNWAY_DATABASE_URL`) and the worker.

### `expansion/k-engine` (P2) - partial, WIP 1396248
- Brief: `build-log/prompts/rwy-engine.md` (plus overrides: build A media wiring last on top of
  the merged media core; migration `0017_publishing_engine` down `0016_shared_contracts`).
- WIP has: `0017_publishing_engine.py`, `posts/` package, `platforms/publisher.py`,
  `publisher_registry.py`, `assisted_publisher.py`, `mock_publishers.py`, `post_settings.py`,
  `publish_capability.py`, model and state-machine edits. **ruff reports 130 issues; nothing is
  tested.** The builder stopped at "the target snapshot (payload, hash, publisher resolution,
  validation issues)". Treat as a draft: review, finish every package A-E and every acceptance test
  in the brief.

### `expansion/k-agent` (P5) - partial, WIP d71b40c
- Brief: `build-log/prompts/rwy-agent.md` (overrides: key resolution in a separate
  `runway.auth.api_keys.resolve_api_key` called from the marked `rwy_` spot; MCP SDK `mcp`, newest
  release at least two weeks old that serves 2025-11-25 and 2026-07-28, pinned exactly; post/media
  services behind a Protocol until the engine merges; migration `0019_agent_interface`).
- WIP has: `auth/api_keys.py`, `audit.py`, `key_limits.py`, `release_policy.py`,
  `request_policy.py`, `events/visibility.py`, `agent/` package, `0019_agent_interface.py`, edits
  to principal, settings, models, events. **ruff reports 37 issues; untested.** Stopped at "agent
  services Protocol, registry, and the approvals queue". MCP server and outbound webhooks not
  confirmed built.

### `expansion/k-realtime` (P4) - not started
- Brief: `build-log/prompts/rwy-realtime.md` (with the orchestrator note at the top: merge
  `expansion/k-p6` for the standby rule; migration `0018_webhook_inbox`). The Kiro job ran ~5 min
  and left nothing.

### Not yet briefed (write and run after the above)
- **Wave 3** (briefs ready): `rwy-youtube.md` (YouTube publishers incl. `youtube_post`, incremental
  consent), `rwy-social.md` (Instagram incl. 9007 rule and R2 temp copy; TikTok inbox/direct and the
  mandatory screen), `rwy-web.md` (post editor, release dialog, approvals, keys, calendar, SSE,
  freshness, typed client from OpenAPI with a drift check).
- **Wave 4** (no brief yet; spec 01 §5-§6, 05 §3-§5, IMPLEMENTATION-PLAN P7/P8):
  Postgres support (pinned `psycopg` driver, `RUNWAY_DATABASE_URL`, settings validation), a CI job
  with a Postgres service running the API suite on both databases, `WorkspaceScoped` mixin with
  `with_loader_criteria` + `before_flush` guard, RLS migration and roles (Postgres only),
  isolation test suite for every scoped model and route, database token store with key versions
  and `runway tokens rewrap`, disconnect/delete/export flows and platform deauthorize callbacks,
  audit-log view, direct-to-storage upload wiring in hosted, Instagram Facebook-Login-for-Business
  adapter (resumable upload from disk; renamed from "Unified Login", see recheck §2).
- **Production hardening for the owner's connect step:** `runway doctor platforms` (per platform:
  client id/secret present, redirect URI, public edge reachable for TikTok, scopes, token state,
  what is missing in plain words); `docs/CONNECT_ACCOUNTS.md` (exact Google Cloud, Meta and TikTok
  console steps with the redirect URIs Runway actually uses -
  `/api/platforms/{platform}/oauth/callback` on `oauth_redirect_base_url`, Tailscale Funnel steps,
  where secrets go via the private instance, then live tests Y-1..Y-5, I-1..I-4, T-1, T-2 from
  IMPLEMENTATION-PLAN, written as owner steps).

## 5. Review findings (independent Opus reviewers, read-only, with reproductions)

Full findings with file:line are in the fix briefs (`build-log/prompts/*-fix1.md`).

**Media core (11 findings, all fixed, merged):** (1) HLS/concat demuxers followed `file:` links
past `-protocol_whitelist file` - a crafted `.m3u8` read another workspace's video and its poster
(reproduced with ffmpeg 7.1.1) → `-format_whitelist`, server-named inputs, server-chosen
extensions; (2) probe errors leaked absolute paths; (3) R2 never verified SHA-256; (4) keys not
bound to workspace, mixed-case aliasing on Windows; (5) atom parser reported "no edit list" after
exhausting its budget; (6) single-entry edit lists ignored; (7) upload locks per instance only;
(8) stale cleanup by start time; (9) R2 stored client Content-Type; (10) TikTok planner allowed a
>64 MB single chunk; (11) R2 ranged read and parallel-complete edge cases. Verified correct: SigV4
against AWS vectors, path-traversal rejection, validation table vs spec 02 §5, chunk boundaries.

**P6 (7 important + 3 nits, fixes committed, not re-reviewed):** push could bypass the visibility
guard via SSH alias origin / `pushurl` / `insteadOf` and publish `secrets.enc.env` and the token
store to a public branch; secret scan missed `GOCSPX-`, `*_CLIENT_SECRET=`, `client_secret` JSON,
`RUNWAY_TOKEN_KEY`, `1//` and `rft.` refresh tokens, `IGAA` tokens, UTF-16 files, snapshot files;
scan only after publication; `machine_id` settable in shared `instance.toml`; stale-lease takeover
left two active machines; standby did not block writes or the worker; `pull` took the lease before
restoring; `instance.toml` deny-list allowed executable paths; no subprocess timeouts; instance dir
could be another checkout of this public repo.

**P0 (3 deploy blockers + others; mostly fixed, items 2/3/5/11 in WIP):** migrations not crash-safe
on SQLite (pysqlite commits the first CREATE outside the transaction; a crash left
`_alembic_tmp_channels` and every rerun failed; 0013 skipped tables after a partial run) -
reproduced; a worker start failure made the launcher stop the healthy API (Supervisor restart
loop); unsafe deploy/rollback order (old API + new worker could both refresh TikTok tokens; no
`db downgrade`; external supervisors do not stop the worker); Postgres breakers (`recreate="always"`,
sequence not advanced after explicit id 1); worker loop robustness; backwards draft status;
cross-process sync race; unbounded job rows; `#` in data path; tests that cannot fail. Verified
correct: full migration rehearsal 0012→head→0012 with rows in all 15 scoped tables (counts,
indexes, FKs, uniques, defaults preserved; migrated schema equals model schema), `BEGIN IMMEDIATE`
really locks, refresh lock ordering, route walk, hosted 401 before handlers.

**Not reviewed yet:** s-hosted, P6 fixes, P0 fixes, media fixes, engine, agent, everything after.
Every branch gets an independent read-only review before merge (it found real bugs every time).

## 6. CI runs (GitHub Actions on ChardXBT/Runway)

| Run | Branch @ commit | Result |
| --- | --- | --- |
| 45062687750 | expansion/build (baseline) | success |
| 45062678973 | k-p6 (before fixes) | success |
| 45062674372 / 45067601092 | k-media before / after fixes | success / success |
| 45062684012 | k-p0 (0.1-0.6) | failure: literal head assertion in `tests/integration/test_foundation.py` |
| 45066533417 | k-p0 @ 404529e | success |
| (pending) | k-p0 08cf900, k-p6 82c8939, k-engine, k-agent WIP, s-hosted, build 059d636+ | check with `gh run list -R ChardXBT/Runway` |

WIP branches are expected to fail lint/tests.

## 7. How the background work was run (to rerun or replace)

- **Kiro jobs** (`kiro-cli`, Opus 5.5 high, all tools trusted) with the watchdog from the ai-skills
  library `capabilities/kiro-cli-agents/scripts/kiro_watchdog.sh`. Wave folder
  `%USERPROFILE%\Downloads\kiro-wave-20261122-runway` (copy in `build-log/`): `COMMON-RULES.md`
  (fences every job must follow), `prompts/`, `jobs-wave*.txt` (`name|unix-cwd|windows-cwd|prompt|start`),
  `orchestrate-wave*.sh` (launched detached with `Start-Process -WindowStyle Hidden bash.exe -lc
  "bash <script> >> <out> 2>&1"`), `LAUNCH_LOG.md`, `<job>/kiro.log`, `<job>/REPORT.md`.
  `resume_with.sh <job> <unix-cwd> <win-cwd> <message-file>` resumes a finished job's own session
  with a follow-up brief and re-adopts it in a watchdog. `stop_wave2.ps1` stops one job and its
  watchdog safely (match the job marker, never a broad command-line match).
- Limits on this laptop: at most 3 Kiro jobs at once; Kiro processes lose the ability to start
  children after 10-70 minutes (`0xC0000142`) - the watchdog kills and resumes them; RAM is tight
  (1.3-1.7 GB free with bots running), so never run the full suite locally; Kiro account is the
  school Builder ID (`kiro-cli whoami`); only the owner switches accounts.
- **Claude subagents** (Opus) were used for builders (hosted seam, engine, agent) and for every
  independent review. Both builders died at ~15:45 on the Claude 5-hour limit with uncommitted work
  (saved as the WIP commits). Builders must commit after every package.
- CI watch: a loop over `gh run list -R ChardXBT/Runway --json databaseId,headBranch,status,conclusion`
  printing newly completed `expansion/*` runs.
- No background jobs are running now: all Kiro jobs and watchdogs ended with the previous session.

## 8. Mistakes made (and the rule learned)

1. Python heredocs with Windows paths in Git Bash fail (`\U` unicode escape) - it happened twice;
   the second time a prompt edit silently failed and a job launched with an outdated prompt (caught
   within a minute, stopped, relaunched). Write such files with a file-writing tool, or use raw
   strings, and check the edit before launching.
2. `sed` cannot replace across `\n` - use a small Python script or the editor tool.
3. The first plan ran the 40-minute full suite locally under a shared lock; three jobs queued
   behind it for over an hour and RAM fell to 1.3 GB. Switched to GitHub CI for the full suite.
4. A job left `heavy.lock` behind; it had to be removed by hand. Locks need an owner PID and a
   stale check.
5. To re-scope running jobs, the orchestrator wrote a placeholder `Done:` REPORT to release the old
   watchdog; P0's and P6's original reports were never written as a result (their content is in
   commits only). Stop a watchdog with a targeted stop script instead.
6. Two CI/event monitors ran at once and duplicated events; stop the old one first.
7. Creating three venvs in parallel failed once (pip contention); create them one at a time.
8. Builders (subagents and Kiro) did not commit often enough; a usage limit and a session end left
   large uncommitted diffs. Require a commit per work package.
9. The first realtime prompt assumed P6 was on its base; it was not. Always state the exact base
   commit and what it lacks.
10. Spec 08 named Meta's newer path "Unified Login"; the re-check found no such name. The newer
    path to target is Instagram API with Facebook Login for Business (resumable upload from disk).

## 9. Remaining work, in order

1. **Finish P0** (`k-p0`): complete fix items 2, 3, 5, 11 on top of the WIP; ruff/mypy; targeted
   tests + `pytest tests/unit`; push; CI green; independent review of fb71830..HEAD; fix; merge into
   `expansion/build`.
2. **Verify P6** (`k-p6`): check items 7 and 10 of its fix brief; write its report; CI green;
   independent review of the fixes (especially the push-URL guard, lease, standby, secret-scan
   history range); merge after P0.
3. **Merge s-hosted** after P0 (drop the 0013 copy commit, re-chain 0016h); independent review.
4. **Finish the engine** (`k-engine`) from its WIP against the brief; then **agent** (`k-agent`);
   then **realtime** (`k-realtime`, from scratch). Each: review, CI, merge; re-chain migrations
   0017 → 0018 → 0019; connect the agent's service Protocol to the engine's real services.
5. **Wave 3:** YouTube, Instagram/TikTok publishers, web UI (briefs ready). Review, CI, merge.
6. **Wave 4:** Postgres + CI matrix, tenancy enforcement and RLS, isolation tests, DB token store,
   deletion/export/deauthorize flows, audit view, IG Facebook-Login adapter. Review, CI, merge.
7. **Connect-flow hardening:** `runway doctor platforms`, `docs/CONNECT_ACCOUNTS.md`,
   `PLATFORM_CAPABILITIES.md` regenerated, `VERIFICATION_REPORT.md` updated ("built and
   mock-tested; live tests owed").
8. **Integrated review** of `expansion/build` vs `origin/main` (whole diff, several lenses:
   security, migrations, compatibility of every existing route and the Supervisor health checks,
   Muse compatibility), then fix.
9. **Migration rehearsal on a copy of production:** find the production database through the live
   checkout's configured data directory (read-only; the specs describe it as ~470 MB), copy it with
   SQLite's online backup API to a scratch folder outside any repo, run `runway db upgrade` from the
   build branch against the copy, compare row counts for every table, run the app against the copy
   on spare ports and click through `/review`, `/lineup`, `/queue`, accounts, drafts. Then
   `runway db downgrade 0012_creator_platforms` on a second copy and compare.
10. **Ship:** PR `expansion/build` → `main` (body ends with the Claude Code line if Claude writes
    it), CI green, merge. **Deploy** to `%USERPROFILE%\Downloads\RunWay`: stop Runway (API on 8000,
    web on 3000, and any worker) cleanly; back up the production DB with the online backup API;
    `git pull --ff-only` on `main` (the live checkout has local untracked logs; keep them);
    `.venv\Scripts\python.exe -m pip install -r requirements.lock` then `--no-deps -e .`;
    `npm ci` at root and `apps/web` if lockfiles changed and rebuild web as the launcher expects;
    `runway db upgrade`; start with `run-runway.ps1`; confirm both `/health` endpoints return
    `product=Runway status=ok` within 45 s (the Supervisor's readiness timeout, in
    `bot-supervisor/bot_supervisor/local_apps.py`; it only launches Runway when health fails and
    never stops it); confirm the worker runs; check `/review` and `/lineup` still work. Roll back
    with the documented downgrade if anything fails.
11. **Record:** update `docs/VERIFICATION_REPORT.md`, this handoff, and hand the owner the
    connect runbook.

## 10. Owner actions (only the owner can do these)

- Create the Google Cloud project (YouTube Data + Analytics APIs, OAuth consent "In production",
  web client with Runway's redirect URI), the Meta developer app (Instagram API with Instagram
  Login; his account as tester), the TikTok developer app (Login Kit, Display API, Content Posting
  upload; sandbox with his account).
- Turn on Tailscale Funnel for the TikTok callback and webhooks only.
- Optional: Cloudflare R2 bucket (needed for Instagram publishing from a local install).
- Create the private `runway-instance` repo, install `sops` and `age`, run `runway instance init`.
- Enable GitHub secret-scanning push protection on ChardXBT/Runway.
- Connect his accounts, enable publishing, run live tests Y-1..Y-5, I-1..I-4, T-1, T-2.
- Hosted (later, only if wanted): Clerk app, domain, host, Postgres; Meta Business Verification
  needs a registered business or sole proprietorship; TikTok audit; Google verification.
- Apply for the YouTube `posts` API pilot when Runway is a real product.

## 11. Facts worth keeping

- Supervisor: launches Runway via `run-runway.ps1` only when either health check fails; waits 45 s;
  never stops Runway. A long first migration must therefore run in a manual stop/backup/upgrade/
  start window, not through the Supervisor.
- No local Postgres (Docker daemon not running; no WSL distro besides docker-desktop): test
  Postgres in CI with a service container.
- ffmpeg/ffprobe 7.1.1 are on PATH (WinGet Gyan.FFmpeg).
- `mcp` Python SDK versions on PyPI on 2026-11-22: 2.3.0, 2.2.0, 2.1.x, 2.0.x, 1.30.0, ...
- Clerk session token: v2 `o: {id, rol, slg, per, fpm}`, v1 `org_id`, `org_role` (`org:` prefix),
  `org_slug`; `azp` may be absent.
- Platform facts re-read on 2026-11-22: `research/recheck-20261122.md`.
