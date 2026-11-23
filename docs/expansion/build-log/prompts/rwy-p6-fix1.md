Job rwy-p6: STOP waiting for the heavy lock; do not run the full suite (COMMON-RULES rule 7 changed:
GitHub CI already ran the full suite on your branch and it passed). An independent security review
of your branch found the problems below. Fix all of them in the same worktree
(`%USERPROFILE%\Downloads\RunWay-k-p6`, branch `expansion/k-p6`), same rules. Each fix gets a test
that fails before the fix. Then run ruff, ruff format, mypy, every instance/secret-scan/settings
test file, and the unit set (rule 7), commit, and write REPORT.md (first line the verdict) with a
"Review fixes" section listing each item and its test.

1. IMPORTANT: the visibility guard does not cover the effective push destination
   (`src\runway\operations\instance.py` ~317-354, ~383, ~402). `git push -u origin HEAD` honours
   `pushurl`, `insteadOf` and `pushInsteadOf`, and when origin cannot be parsed (SSH host alias like
   `git@github-chard:ChardXBT/Runway.git`, `ssh://...:443/...`, a local path) `_github_name` returns
   None and the mismatch check is skipped. Fix: never push to a remote name; push to
   `https://github.com/<verified canonical owner/name>.git` explicitly (credential helper of `gh`),
   and additionally refuse when `git remote get-url --push origin` (or any configured push URL)
   cannot be parsed or does not equal the verified canonical name. Tests: SSH alias origin,
   `pushurl` pointing at the public repo, `insteadOf` rewrite.
2. IMPORTANT: `scripts\secret_scan.py` (~43-58, ~102) misses the formats this feature handles:
   `RUNWAY_YOUTUBE_CLIENT_SECRET=GOCSPX-...` / `youtube_client_secret=...` (the lookbehind rejects
   `_` before CLIENT_SECRET), Google `client_secret_*.json` (`"client_secret": "..."`), Google
   `GOCSPX-` secrets anywhere, `RUNWAY_TOKEN_KEY=` / `RUNWAY_TOKEN_KEYS=` with a value, Google
   refresh tokens `1//...`, TikTok refresh tokens `rft.`, Instagram `IGAA...` / `IGQV...` tokens,
   UTF-16 files (decode UTF-16 when a BOM or NUL pattern says so instead of skipping as binary),
   and snapshot files `*.db.gz`, `runway-operational-media.zip`, `*.db-wal`, `*.db-shm`,
   `*.sqlite`, `*.sqlite3`. Also add those snapshot patterns to `.gitignore`. Keep the current tree
   passing (narrow, reviewed allow-list entries only).
3. IMPORTANT: the scan only runs after publication. In `.github\workflows\ci.yml` scan every commit
   in the pushed range (checkout with `fetch-depth: 0`; for pull requests the PR range; for pushes
   `github.event.before..github.sha`, falling back to the whole history when `before` is all
   zeros), not only HEAD. Make the pre-commit hook easy to install via `core.hooksPath` (a
   `scripts\install_hooks.ps1` and a `.githooks\pre-commit` that runs the scanner on staged
   content). In `docs\PRIVATE_INSTANCE.md` tell the owner to enable GitHub secret scanning push
   protection on the public repository (owner action).
4. IMPORTANT: `machine_id` can be set in the shared `instance.toml`
   (`src\runway\config\instance.py` ~30-44, `instance_lease.py` ~104), so every machine can claim
   the same identity. Make `machine_id` environment-only (refuse it in `instance.toml` and in any
   shared layer); default to a per-machine id stored in the user profile (not hostname alone, which
   can collide), created once.
5. IMPORTANT: stale-lease takeover leaves two active machines (`instance_lease.py` ~58-63,
   ~166-179): the heartbeat only advances on a manual push and the old holder only reads its local
   `lease.json`. Fix: (a) the active machine refreshes the heartbeat periodically (a worker job or
   the launcher; at least every 6 h) by committing and pushing `lease.json`; (b) before any token
   refresh, platform write or worker start, a machine re-reads the lease from the remote when the
   local copy is older than a short interval (e.g. 10 min) and drops to standby if it no longer
   holds it; (c) `--take` requires the lease to be stale by the REMOTE copy. Tests for each.
6. IMPORTANT: standby does not block platform writes or the worker. `fresh_token`
   (`platforms\registry.py` ~94) returns an unexpired token with no check, so inbox replies
   (`inbox.py` ~276) and any publishing still run on a standby machine; nothing creates
   `runtime\worker.busy`, so `wait_for_idle_worker` never waits. Fix: gate every platform write path
   and token use for writes through `instance_is_active` (one function; a `StandbyError` mapped to a
   clear API error), refuse to start the worker on a standby machine (the worker module lives on
   another branch; expose `ensure_active_or_raise()` and document in the report that the worker
   must call it at start and before each job; also make the launcher skip starting the worker in
   standby if the launcher has a worker on your branch), and make "worker busy" real: a function
   pair `mark_worker_busy()/clear` the worker will call, plus `wait_for_idle_worker` honouring it
   with a timeout. Spec 07 §9: "machine B cannot refresh tokens or run the worker".
7. IMPORTANT: `pull` takes the lease before restoring (`instance.py` ~1044 vs ~1056): download and
   verify FIRST, then take the lease, then install. Push uploads to `snapshot-latest` with
   `--clobber` before the git push that arbitrates: arbitrate (push the lease/manifest commit) first,
   then upload, and use dated tags `snapshot-YYYYMMDD-HHMMSS` plus updating `snapshot-latest`, keeping
   the newest N (setting, default 5). `pull` by the current holder must refuse to restore over a
   local database newer than the snapshot unless `--force`, with the local DB backed up first.
8. Nit (do it): `instance.toml` uses a deny-list; switch to an explicit allow-list of non-secret,
   non-executable settings (profile, channel name, time zone, ports, modes). Executable paths
   (`codex_cli_path`, `publisher_chrome_path`), `agent_runtime`, `oauth_redirect_base_url` and
   `data_dir` must not be settable from the shared file.
9. Nit (do it): timeouts on every `sops`, `gh` and `git` call (`GIT_TERMINAL_PROMPT=0`); avoid
   plaintext in `%TEMP%` for `_encrypt_secrets` (pipe via stdin, or create the temp file inside the
   instance directory's ignored `tmp\` with an ACL restricted to the current user, and delete in a
   `finally`); scrub `sops` stderr before it goes into `InstanceConfigError`.
10. Nit (do it): `resolve_instance_dir` must refuse ANY git working tree of the public code
    repository (check `git -C <dir> rev-parse --show-toplevel` and its remotes, and refuse a
    directory inside any checkout whose remote is `ChardXBT/Runway`), not only this checkout.
