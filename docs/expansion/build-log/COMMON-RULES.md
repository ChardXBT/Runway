# Common rules for every Runway expansion Kiro job (wave 2026-11-22)

You are implementing part of the Runway expansion. The specifications are in your worktree under
`docs\expansion\` (start with `docs\expansion\specs\00-principles.md`; its invariants override
everything else). Repository rules are in `AGENTS.md`, except: you work on your own branch, not on
`main`, and you never push.

## Hard fences (breaking any of these ruins the job)

1. Change files ONLY inside your own worktree (named in your prompt). Never edit, run, or read data
   from the live checkout `%USERPROFILE%\Downloads\RunWay` (it is the production instance), its
   `data\` folder, or any `.env` file. Never copy or open the production database.
2. Never run `run-runway.ps1`, `A_Runway.bat`, the Supervisor, or any bot. Never start, stop, or
   kill a process you did not start yourself. Ports 8000 and 3000 belong to the live Runway; if a
   test needs a server, use a random free port.
3. No network calls to YouTube, Google, Meta/Instagram, TikTok, Cloudflare or any real platform.
   No sign-ups, no account creation, no money, no secrets. Tests use mock providers, fakes and
   temporary directories only (invariant 9).
4. Do not `git push`, do not merge into other branches, do not touch other worktrees. Commit to your
   own branch with clear messages. The orchestrator reviews and merges.
5. Your shell tool runs **PowerShell**, not bash. For a bash-only command use
   `& "C:\Program Files\Git\bin\bash.exe" -lc "..."`.
6. Use your worktree's own virtual environment: `.\.venv\Scripts\python.exe -m pytest ...`,
   `.\.venv\Scripts\python.exe -m ruff ...`, `.\.venv\Scripts\python.exe -m mypy src/runway`.
   Do not `pip install` new packages into it unless your prompt allows a new dependency; if you
   add one, add it to `pyproject.toml` and `requirements.lock` with an exact version.
7. Test scope (changed 2026-11-22 13:45 for wave 2 onward): do NOT run the full backend suite
   locally; it takes about 40 minutes on this laptop. The orchestrator pushes your finished branch
   and GitHub CI runs the full suite there. Locally, run: every test file you added or changed, the
   test files covering every module you changed (find them with `Select-String` over `tests\`),
   `tests\unit\test_windows_launcher.py` if you touched the launcher, and the fast regression set
   `.\.venv\Scripts\python.exe -m pytest tests\unit -q -x -p no:cacheprovider` (unit only). Report
   exactly which test commands you ran and their counts.

## Engineering rules

- Read the spec sections named in your prompt before writing code, and the existing code they
  reference. Match the existing style (type hints, `mypy` clean, `ruff` clean and formatted).
- New behaviour gets a test that fails before the change. Keep every existing test passing.
- Existing routes, the CLI and the `AgentRuntime` protocol keep their behaviour in the `local`
  profile (invariant 8). The `local` profile must never need a login, a cloud account or a paid
  service (invariant 17).
- Never log or return tokens or secrets (invariant 5).
- Prefer small, well-named modules. Do not rewrite unrelated code.
- If the spec is ambiguous, choose the safer reading, write it down in the report under
  "Deviations and decisions", and keep going. You cannot ask questions; put owner questions in the
  report.
- Before finishing: `ruff check .`, `ruff format --check .`, `mypy src/runway`, your new and affected tests and the fast unit set (rule 7). If something you did not touch already
  fails on the base commit, say so with evidence (run it on a clean `git stash`-free checkout of the
  base commit is NOT allowed; instead show that the failure is unrelated to your diff).

## Report

Write `%USERPROFILE%\Downloads\kiro-wave-20261122-runway\<your job name>\REPORT.md`. Its FIRST
line is the verdict:

- `Done: <one sentence>` when every work package in your prompt is built, tested and committed;
- `Partial: <one sentence>` when some part is deliberately left (say exactly which and why);
- `Blocked: <reason>` when you could not proceed. If your shell stops starting processes
  (error `0xC0000142`), write `Blocked: 0xC0000142` and stop.

Then: commits (hash + subject), files added/changed, migrations (revision ids and down_revision),
tests run with pass/fail counts and durations, ruff/mypy results, deviations and decisions, owner
questions, and what the next job must know.
