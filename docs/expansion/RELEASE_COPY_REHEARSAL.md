# Expansion release-copy rehearsal and owner acceptance checklist

**Status: OWNER HOLD.** This is a release-readiness package, not a release approval. Do not
publish a GitHub release, deploy the expansion, connect OAuth, enable publishing, merge the
integrated branch, or run the production-copy commands against anything except the owner-confirmed
production database location. The single owner action is in the final section.

Prepared from draft PR #16 head `17fedaf4701b9f9adeca1bd73952bac5bc1e319d` on base
`4f257217146a3d5635ebeb608d024eb91d2df0a9`, using issues #11, #14 and #18. The existing
integrated branch was not edited.

## Evidence classes

- **generated**: produced here on synthetic SQLite rows; proves migration mechanics only.
- **reported**: already reported in PR #16 / issue evidence; not independently repeated here.
- **owner-verified**: acceptable for release only after Eric performs it on the owner-confirmed
  production copy or live account.

## Independent generated-data rehearsal (2026-11-26)

Toolchain: Linux, Python 3.12.16, candidate CLI from PR #16 head `17fedaf4701b`.
The source database was generated at `0012_creator_platforms`, then populated with:

- duplicate semantic content rows (`dup-video-a` / `dup-video-b`, same title, caption and raw
  payload; the 0012 schema already enforces account/external-id uniqueness, so exact duplicate
  external ids cannot exist);
- orphaned rows (`content_metric_snapshots.content_id=999`, `comment_records.content_id=999`,
  `reply_drafts.comment_id=999`, `draft_targets.published_content_id=999`);
- timezone-bound rows on `America/Toronto`: the DST-fold instant `2026-11-01 02:30:00.000000` and
  the nonexistent DST-gap wall time `2026-03-08 02:30:00.000000`.

A first triple-check reproduction used fixture channel handle `@northwind` while the candidate
defaults to `Qlob`; the public CLI correctly created the missing default channel, so `channels`
changed from 1 to 2 and the rehearsal failed closed. After correcting the generated fixture to the
actual configured channel identity (`Qlob`), the isolated run passed.

Exact passing command:

```bash
/home/ubuntu/runway-pr16-candidate/.venv/bin/python scripts/rehearse_migrations.py \
  --source /home/ubuntu/runway-rehearsal-scratch/generated-base/data/runway.db \
  --candidate /home/ubuntu/runway-pr16-candidate \
  --scratch /home/ubuntu/runway-rehearsal-scratch/run2
```

Exact generated results:

| Check | Result |
| --- | --- |
| original revision / tables / rows / integrity | `0012_creator_platforms` / 66 / 24 / `ok` |
| copy A `runway db upgrade` exit / revision / tables / integrity | 0 / `0020_youtube_publishing` / 91 / `ok` |
| copy A original-table row-count differences | none |
| copy A `PRAGMA foreign_key_check` | 4 rows, exactly the intentional orphan fixtures |
| copy B upgrade + `runway db downgrade 0012_creator_platforms` exits | 0 / 0 |
| copy B final revision / tables / integrity | `0012_creator_platforms` / 66 / `ok` |
| copy B changed counts / extra tables / missing tables | none / none / none |
| copy B `PRAGMA foreign_key_check` | 4 rows, exactly the intentional orphan fixtures |
| duplicate rows after downgrade | both `dup-video-a` and `dup-video-b` retained |
| timezone rows after downgrade | DST-fold and DST-gap strings retained byte-for-byte |
| script verdict | `REHEARSAL PASSED` |

The candidate report is at `run2/rehearsal-20261126T223428Z/report.json` on the verification VM;
copies and reports contain synthetic data only and were not committed.

### Independent review correction and rerun

The first draft of this checklist named `scripts/rehearse_migrations.py` but did not commit that
script, so the owner handoff command could not run from the PR branch. The script is now committed
in this branch. It opens the source read-only, copies it twice with SQLite's online backup API,
refuses a non-empty scratch directory rather than deleting anything, migrates only the copies, and
writes a row-count/schema/row-content report without copying row values into the report. For the
rollback copy it compares semantic schema structure (columns, foreign keys and indexes, ignoring
constraint creation order) and canonical row contents, not just row counts.

Independent generated rerun on Linux Python 3.13 with the committed script: exit 0,
`REHEARSAL PASSED`; source/copy-B `0012_creator_platforms`, copy-A `0020_youtube_publishing`;
source and copy-B 66 tables / 10 rows, copy-A 91 tables / 11 rows; no original-count, schema,
row-content, missing-table or extra-table differences; integrity `ok`; exactly the 4 intentional
orphan foreign-key rows. The generated fixture contains duplicate semantic rows, orphan rows and
America/Toronto DST-fold/DST-gap strings. Focused script coverage: 2 passed, including the
non-empty-scratch fail-closed case.

## Isolated publish-off health check

The health check used FastAPI's in-process `TestClient` against the upgraded generated copy with
`RUNWAY_PLATFORM_PROVIDER=mock` and the scratch data directory. It did not bind a public port,
touch OAuth, connect a platform, start the sync scheduler, or submit a publish job.

Exact health fields:

```json
{
  "status": "ok",
  "product": "Runway",
  "publishing_enabled": false,
  "publishing_mode": "assisted",
  "platform_provider": "mock",
  "migration_head": "0020_youtube_publishing",
  "sync_scheduler": "worker"
}
```

Supporting generated facts: `publish_jobs=0`, `publishing_controls=0`,
`youtube_automation_authorized=False` in the startup log, and no publish attempt was made. This is
an isolated end-to-end-equivalent health check, not a live deployment smoke.

## Existing reported evidence to reuse

| Evidence | Class | Current result / use |
| --- | --- | --- |
| PR #16 backend suite | reported | 2062 passed, 31 skipped after the OAuth workspace fix |
| PR #16 frontend check | reported | `npm run check` passed: Vitest 46 files / 228 tests, ESLint, `tsc --noEmit`, OpenAPI drift check and Next build |
| PR #16 Ruff / mypy | reported | Ruff and format passed on changed files; mypy passed on 253 source files |
| PR #16 synthetic migration | reported | upgrade to `0020_youtube_publishing`, downgrade to `0012_creator_platforms`, integrity and foreign-key checks clean on fixture data |
| Independent generated edge rehearsal | generated | passed as above |
| This branch backend suite (docs-only change) | generated | `.venv/bin/python -m pytest -q -n 6 --dist loadfile`: 2065 passed, 32 skipped in 207.33s; Windows/PowerShell-only cases skipped on Linux |
| Publish-off health | generated | passed as above |
| Production-copy migration | owner-verified | **not done**: production database location is still owner-confirmed only |
| OAuth live acceptance | owner-verified | **not done**: owner-only account/console work |

## Exact production-copy commands for Eric

Run these on the machine that can see the real production data directory. Replace only the three
paths in angle brackets. Do not commit, upload, attach, or paste any copy, report containing real
rows, `.env`, token, browser profile, media, or database. The scratch folder must be outside every
repository.

PowerShell, from a PR #16/release-candidate checkout that already has `.venv` installed:

```powershell
$Candidate = "<absolute path to release-candidate checkout>"
$SourceDb = "<absolute path to live data dir>\runway.db"
$Scratch = "<absolute path outside every repo>\runway-migration-rehearsal"

& "$Candidate\.venv\Scripts\python.exe" "$Candidate\scripts\rehearse_migrations.py" `
  --source $SourceDb `
  --candidate $Candidate `
  --scratch $Scratch
```

Expected pass conditions are exactly the script's pass conditions:

1. copy A upgrades through the public CLI to `0020_youtube_publishing` with exit 0;
2. every table present in the production source has an unchanged row count after upgrade;
3. copy B upgrades then downgrades to `0012_creator_platforms` with exit 0;
4. copy B has no changed row counts, no missing tables and no extra tables versus the source;
5. both final copies report `PRAGMA integrity_check = ok`;
6. foreign-key violations, if any, are explicitly counted and investigated before deploy — they are
   evidence of pre-existing production data damage, not something to hide or repair by hand;
7. the final line is `REHEARSAL PASSED`.

If it fails, stop deployment and keep the failing scratch copy private for diagnosis. Do not retry
against the live database and do not hand-edit rows.

## Deploy/rollback command order after the copy passes

These are the release-candidate commands; run them only after Eric explicitly approves deployment.
They are included here so the release draft is complete, not as permission to execute them.

```powershell
# 1. Back up the live database with SQLite's online backup API before changing code or schema.
#    Use the documented backup command/procedure in the candidate checkout.

# 2. Stop the API and worker through the supervisor/launcher, then confirm no worker holds:
#    <data>\runtime\worker.lock

# 3. From the NEW checkout, upgrade the configured copy/database:
& .\.venv\Scripts\runway.exe db upgrade

# 4. Start the NEW API and worker; verify /health reports:
#    migration_head = 0020_youtube_publishing
#    sync_scheduler = worker
#    publishing_enabled = false
#    platform_provider = live only when Eric intentionally configures it

# 5. Rollback, if needed, while still using the NEW checkout's downgrade logic:
& .\.venv\Scripts\runway.exe db downgrade 0012_creator_platforms

# 6. Only after downgrade exits 0, restore/start the OLD code. Never run old code against the
#    newer schema.
```

Rollback criteria: stop and roll back if the upgrade exits nonzero, `/health` does not report the
expected migration head, the worker does not start cleanly, row counts differ, integrity is not
`ok`, OAuth acceptance cannot complete without publishing, or any publish/send path appears active
while `publishing_enabled` remains false.

## OAuth acceptance evidence matrix for Eric

All rows are **owner-verified only**. Offline/mock evidence is not acceptance. Publishing stays off
unless Eric separately authorizes a specific live publish test.

| Test | Platform | Required evidence before release approval | Status |
| --- | --- | --- | --- |
| Y-1 | YouTube | connect completes to the correct workspace/account; doctor shows required scopes and redirect URI; no secret printed | owner pending |
| Y-2 | YouTube | private upload acceptance evidence with exact video id redacted/kept private and publishing controls verified | owner pending |
| Y-3 | YouTube | scheduled upload evidence and correct time handling | owner pending |
| Y-4 | YouTube | reconcile after restart/crash-safe state, no duplicate send | owner pending |
| Y-5 | YouTube | quota and limit behavior; unverified-app cap/warning understood | owner pending |
| I-1 | Instagram | Instagram Login connect completes to correct account/workspace | owner pending |
| I-2 | Instagram | image/carousel acceptance evidence within capability limits | owner pending |
| I-3 | Instagram | Reel/media staging evidence, including R2 if used | owner pending |
| I-4 | Instagram | webhook/deauthorize/data-deletion and limit behavior verified | owner pending |
| T-1 | TikTok | connect and fresh creator-info read succeed through the approved edge | owner pending |
| T-2 | TikTok | inbox upload completed manually in the TikTok app; no direct posting beyond approved capability | owner pending |

## Records still needed before release

From #11/#13/#14/#18, the release record still needs:

1. the owner-confirmed production host and exact production `RUNWAY_DATA_DIR` / database path;
2. the passing production-copy `report.json` summary (counts and hashes only; not private rows);
3. owner-verified OAuth rows Y-1..Y-5, I-1..I-4, T-1 and T-2;
4. final exact-head/base SHA deployment approval after the owner reviews the integrated branch;
5. confirmation that no release artifact includes production data, secrets, browser profiles or media.

## Owner handoff

Eric, please provide or confirm the exact live `RUNWAY_DATA_DIR` / `runway.db` path, then either run
the production-copy command above yourself or hand off that location for a read-only online-backup
rehearsal. Until that passes and OAuth acceptance is complete, this release draft remains on
**OWNER HOLD** and must not be merged, published or deployed.
