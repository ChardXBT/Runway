Job rwy-p0: an independent review of packages 0.1-0.6 plus GitHub CI found the problems below. Fix
all of them in the same worktree (`%USERPROFILE%\Downloads\RunWay-k-p0`, branch `expansion/k-p0`),
same rules (COMMON-RULES.md, test scope rule 7, no local full suite). Each fix gets a test that
fails before the fix. Commit, then rewrite REPORT.md (first line the verdict) adding a
"Review fixes" section listing each item and its test. This code will be deployed onto a live
single-user instance with a 470 MB SQLite database that a Supervisor restarts through
`run-runway.ps1` and health-checks on 127.0.0.1:8000/:3000, so items 1-3 are release blockers.

1. BLOCKER (reproduced): migrations are not crash-safe on SQLite. With pysqlite the first
   `CREATE TABLE _alembic_tmp_channels` in 0014 is committed outside the migration transaction; an
   interrupted upgrade leaves the DB at 0013 with that temp table, and every rerun fails with
   `table _alembic_tmp_channels already exists`. 0013 has the same class of bug: it checks only
   `"workspaces" not in existing` (0013_workspaces.py ~47), so a crash after creating `workspaces`
   permanently skips `users`, `workspace_members`, `api_keys`, `audit_log`. Fix: make SQLite DDL
   truly transactional in `alembic\env.py` (Python 3.12+ `sqlite3.connect(..., autocommit=False)`
   via `connect_args`, or SQLAlchemy's documented pysqlite recipe: `isolation_level=None` on connect
   plus an explicit `BEGIN` on transaction begin), so a failed revision rolls back completely; AND make
   every new revision idempotent-safe (check each table individually; drop a leftover
   `_alembic_tmp_<table>` before each batch step). Test: inject a failure mid-0014 (and mid-0013),
   then rerun the upgrade to head successfully with identical row counts.
2. BLOCKER: `run-runway.ps1` ~271-283: a worker start failure (`Start-Worker` throws when the worker
   exits within 5 s or a WMI query fails) rolls back EVERY child of the run, including the API that
   already passed its health check, so /health goes down and the Supervisor loops. Also a worker
   started as `.venv\Scripts\runway.exe worker` holds `worker.lock` but is not recognised (its
   command line lacks `runway.cli.main worker`), so a second worker exits with code 3. Fix: a worker
   failure must never stop the API or web; log it, leave the API up, and let the next launcher/
   watchdog run retry the worker. Recognise the worker by its lock file/PID record, not only by
   command-line text. Guard `mark_stale_runs_failed()` at worker start (retry on "database is
   locked", never crash the worker). Update `test_launcher_starts_one_worker_and_rolls_it_back`,
   which currently asserts the wrong behaviour.
3. BLOCKER: deploy and rollback order. (a) On deploy the old API stays healthy so the launcher skips
   the upgrade but starts the new worker, which refuses the 0012 DB; make the launcher run
   `runway db upgrade` whenever the DB is behind head, and if the running API is older code (its
   `/health` reports a different version/revision than the checkout - add the git revision or
   alembic head to `/health` without removing any existing field), stop it cleanly and restart it
   after the upgrade. (b) Add `runway db downgrade <revision>` (refuses below
   `0012_creator_platforms` unless `--force`, takes a SQLite online backup first). (c) Document in
   `docs\OPERATIONS.md`: the deploy order (stop API and worker, `runway db upgrade` with a backup
   first, start), the rollback order (stop, `runway db downgrade 0012_creator_platforms` with the NEW
   checkout, then check out the old code), and that any external supervisor must stop the worker
   (`runway worker`, identified by `data\runtime\worker.pid`/lock) as well as ports 8000/3000. Have
   the worker write `data\runtime\worker.pid` and remove it on exit. (d) The old in-process scheduler
   and the new worker must never both sync: the worker refuses to run sync jobs while an API process
   whose `/health` shows the old in-process scheduler is up (or simpler: the launcher guarantees the
   old API is stopped before the worker starts - choose and test).
4. Postgres (for the hosted profile; CI will get a Postgres job soon): 0014 uses
   `recreate="always"`, which forces copy/drop/rename on Postgres where `DROP TABLE channels` fails
   on foreign keys; use `recreate="auto"`. 0013 inserts workspace `id=1` explicitly, so the Postgres
   sequence does not advance; after the insert, on Postgres run
   `SELECT setval(pg_get_serial_sequence('workspaces','id'), (SELECT MAX(id) FROM workspaces))`.
   Check 0015 and your new 0016 for the same issues and for SQLite-only SQL outside batch mode.
5. CI failure: `tests\integration\test_foundation.py::test_migrations_seed_channel_and_enable_wal`
   asserts the head is the literal `0012_creator_platforms`. Make it (and any other test asserting a
   literal head) compute the head from Alembic's `ScriptDirectory` so future revisions do not break
   it. Search `tests\` for other literal revision ids.
6. Worker loop (`worker.py` ~71): `idle_seconds()` is outside the try, so a DB error there kills the
   worker; and a due job that cannot be claimed (corrupt `payload_json` makes `Job.of` raise inside
   claim) yields a tight traceback loop. Fix both: every iteration is guarded with backoff, and a job
   whose row cannot be decoded is moved to a `failed` state with the error instead of retried hot.
7. `derive_draft_status` (`state_machine.py`) can return PUBLISHED -> PARTIALLY_PUBLISHED because the
   `if delivered:` branch precedes the keep-forward check; reorder so the derived status never
   moves backwards, with a test.
8. A manual sync from the API and a scheduled sync in the worker are only separated by a
   check-then-insert on sync runs because the per-account lock is per process: use the same
   cross-process per-account file lock as token refresh for the whole sync of an account.
9. Unbounded growth: add a periodic `purge` job kind that deletes completed/failed `publish_jobs`
   older than 14 days (keep `send`/`reconcile` history 90 days) and removes stale `refresh-locks\*.lock`
   files not held by any process.
10. The read-only "database behind head" check builds a SQLite URI from the path without escaping;
    a data directory containing `#` (or `?`, `%`) reads as "not migrated". Escape it properly
    (`pathlib.Path.as_uri()` / `urllib.parse.quote`), with a test using such a directory name.
11. Tests: `test_the_api_runs_no_sync_thread_even_with_an_interval` looks for a thread name that no
    longer exists, so it cannot fail - make it assert no sync is scheduled by inspecting the real
    mechanism; `test_worker_stops_cleanly_on_a_signal_and_is_single_instance` was flaky (failed once
    next to the state-machine tests, >60 s) - make it deterministic and fast; the launcher ordering
    tests are string matches - keep them but add at least one behavioural test with a fake process
    runner if the launcher test harness allows it.
