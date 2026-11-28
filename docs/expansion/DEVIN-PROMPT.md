# Prompt for the next agent (Devin, Opus 5.5, high effort)

Paste everything below the line into a new Devin session in Local mode on the owner's laptop.

---

You are taking over an in-progress build of the **Runway expansion** (repository `ChardXBT/Runway`,
public; Python 3.13 FastAPI/SQLAlchemy/Alembic backend, Next.js web app). The owner has given full
permission to finish it: "Want this thing production ready fully finished then I will connect my
accounts." Work until every item in the handoff's remaining-work list is done, verified, merged to
`main` and deployed to the owner's live local Runway. Do not stop to ask for approvals on
engineering steps; ask only for owner-only actions (listed in the handoff, §10).

## Start here

1. Read `%USERPROFILE%\Downloads\RunWay-expansion-build\docs\expansion\HANDOFF.md` completely. It
   is the single source of truth: goal, decisions (do not reopen them), the state of every branch
   and worktree, migration chain, review findings, CI runs, mistakes to avoid, and the ordered
   remaining work (§9).
2. Then read, in the same folder, `specs/00-principles.md` (its invariants override everything),
   `IMPLEMENTATION-PLAN.md`, `research/recheck-20261122.md`, and `AGENTS.md` at the repo root.
3. Job briefs for each piece are in `docs/expansion/build-log/prompts/` (with the shared rules in
   `build-log/COMMON-RULES.md`). Use them as the specification for each work package, including the
   orchestrator overrides noted in the handoff.

## How to work

- Work in the existing worktrees under `%USERPROFILE%\Downloads` (`RunWay-k-p0`, `RunWay-k-p6`,
  `RunWay-s-hosted`, `RunWay-k-engine`, `RunWay-k-agent`, `RunWay-k-realtime`, integration
  `RunWay-expansion-build`), each with its own `.venv`. Create new worktrees from `expansion/build`
  for wave 3 and wave 4. The WIP commits on `k-p0`, `k-engine`, `k-agent` are unverified drafts:
  review them, then finish them.
- Follow the order in HANDOFF §9: finish P0 → verify P6 → merge hosted seam → engine → agent →
  realtime → wave 3 (YouTube, Instagram/TikTok, web UI) → wave 4 (Postgres, tenancy/RLS, isolation
  tests, DB token store, deletion/export flows, Facebook-Login IG adapter) → connect-flow hardening
  (`runway doctor platforms`, `docs/CONNECT_ACCOUNTS.md`) → integrated review → migration
  rehearsal on a COPY of production → PR to `main` → deploy → records.
- Every new behaviour gets a test that fails before the change. Run `ruff check .`,
  `ruff format --check .`, `mypy src/runway`, the affected test files and `pytest tests/unit`
  locally; **do not run the full backend suite locally** (40 minutes and the laptop runs out of
  RAM) - push the branch and let GitHub CI run it (`gh run list -R ChardXBT/Runway`), and require
  green CI before every merge. For the web app, `npm run check` and the production build.
- Commit after every work package (the previous builders lost work by not committing). Push your
  branches. Merge into `expansion/build` only after green CI and an independent review.
- **Independent review before every merge.** Use a separate reviewer pass (a second Devin session or
  a sub-agent with no access to your conclusions) that reads the diff and the spec, reproduces
  risky paths with throwaway scripts, and reports ranked findings; fix every confirmed finding with
  a test. Every review so far found real bugs (a file-read hole through ffmpeg playlists, a push
  path to the public repo, non-crash-safe migrations).
- When merging, re-chain Alembic `down_revision`s so history is linear
  (`0016_shared_contracts` → `0016h_hosted_identity` → `0017` → `0018` → `0019` → later), drop the
  duplicate 0013 commit on `s-hosted`, regenerate the schema contract, and make head-asserting
  tests compute the head.
- Write files that contain Windows paths with a file-editing tool, not shell heredocs (backslash
  escapes break; see HANDOFF §8).
- Optionally use Kiro CLI jobs for parallel work exactly as HANDOFF §7 describes (at most 3 at
  once on this laptop; the watchdog handles `0xC0000142`); you remain responsible for verifying
  their output.

## Hard rules

- Never edit, test in, or run anything from `%USERPROFILE%\Downloads\RunWay` (the live production
  checkout) except the deploy procedure in HANDOFF §9 step 10, and never read or print `.env`
  values. Never copy production data into any repository or public release.
- Never run `run-runway.ps1` or bind ports 8000/3000 except during that deploy. Never start or stop
  the bot Supervisor or other bots; other processes on this laptop are live trading bots - do not
  touch them.
- No real platform calls, no sign-ups, no developer-app creation, no money, no secrets in the repo.
  Tests use mocks and temporary directories only.
- Never publish, reply, or connect an account. Privacy is always explicit; agents propose, humans
  release.
- The repository is public: before each push, run the secret scan from `k-p6`
  (`scripts/secret_scan.py`) once it is merged.
- Models: Opus 5.5 only, never Fable.

## Done means

All phases built and merged to `main` with green CI; the integrated diff independently reviewed with
findings fixed; migrations rehearsed on a copy of the production database with identical row counts
(upgrade and downgrade); the live checkout updated, `runway db upgrade` applied after a backup, both
`/health` endpoints (127.0.0.1:8000 and :3000) returning `product=Runway`, `status=ok` within 45 s,
the worker running, `/review` and `/lineup` working as before; `docs/CONNECT_ACCOUNTS.md` and
`runway doctor platforms` ready for the owner; `docs/VERIFICATION_REPORT.md` and
`docs/expansion/HANDOFF.md` updated with what was done and what the owner must do next (developer
apps, Tailscale Funnel, private instance repo, live tests Y-1..Y-5, I-1..I-4, T-1, T-2). Finish with
a short plain-language summary for the owner: what is live, what he does next, and anything that
could not be verified.
