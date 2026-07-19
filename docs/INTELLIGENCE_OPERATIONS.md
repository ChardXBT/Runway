# RunWay intelligence operations

This runbook covers migration, backup, representation and model lifecycle,
feedback reconciliation, annotation refresh, blind studies, rollback, and the
intelligence database doctor.

It does not authorize YouTube publishing. Intelligence maintenance and
evaluation must run with publishing disabled.

## Safety envelope

For every production-data intelligence command, set all three overrides in the
same terminal:

```powershell
$env:RUNWAY_DATA_DIR = "data/qlob-production"
$env:RUNWAY_PUBLISHING_ENABLED = "false"
$env:RUNWAY_AGENT_RUNTIME = "mock"
```

These process-local values are deliberate:

- `RUNWAY_DATA_DIR` selects the intended database explicitly.
- `RUNWAY_PUBLISHING_ENABLED=false` prevents any scheduling path from being
  available during maintenance.
- `RUNWAY_AGENT_RUNTIME=mock` prevents an unattended Codex or paid model call.

Do not invoke `publisher confirm`, click Accept, or start a publisher worker
during this runbook. Stop the API and web processes before schema migration or
database restoration.

## Read-only status

```powershell
.\.venv\Scripts\runway.exe database schema-status
.\.venv\Scripts\runway.exe database schema-verify
.\.venv\Scripts\runway.exe intelligence representations status
.\.venv\Scripts\runway.exe intelligence feedback status
.\.venv\Scripts\runway.exe intelligence preference status
.\.venv\Scripts\runway.exe intelligence annotations status
.\.venv\Scripts\runway.exe intelligence providers
.\.venv\Scripts\runway.exe database intelligence-doctor
```

`schema-verify` must report `matches: true`. The doctor exits nonzero when any
critical invariant fails.

## Backup before migration

SQLite may use WAL while RunWay is running. Prefer the SQLite online backup API,
or stop all writers and copy the database together with any `-wal` and `-shm`
companions.

The following PowerShell block uses Python's standard-library SQLite backup API
without importing RunWay or starting a service:

```powershell
$source = "data/qlob-production/runway.db"
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$destination = "data/qlob-production/backups/runway-pre-0008-$stamp.db"

@"
import sqlite3
from pathlib import Path

source = Path(r"$source")
destination = Path(r"$destination")
destination.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(source) as reader, sqlite3.connect(destination) as writer:
    reader.backup(writer)
    assert writer.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert not writer.execute("PRAGMA foreign_key_check").fetchall()
print(destination.resolve())
"@ | .\.venv\Scripts\python.exe -

Get-FileHash -Algorithm SHA256 $destination
Get-Item $destination | Select-Object FullName, Length, LastWriteTimeUtc
```

Record before migration:

- database and backup hashes;
- byte sizes and UTC timestamps;
- `PRAGMA integrity_check`;
- `PRAGMA foreign_key_check`;
- migration revision;
- schema fingerprint;
- WAL size;
- preserved row counts for channels, posts, media, annotations, corrections,
  candidates, proposals, and feedback.

Never overwrite or delete the pre-migration backup during validation.

## Rehearse the exact upgrade

1. Copy the verified backup into an isolated temporary data directory.
2. Point `RUNWAY_DATA_DIR` at that directory.
3. Keep publishing disabled and runtime mock.
4. Run `runway init` to upgrade to the current Alembic head.
5. Run schema verification, the intelligence doctor, integrity checks, and
   representative backfills against the rehearsal copy.
6. Confirm that the normalized clean-install and upgraded schemas have the same
   committed fingerprint.

Example:

```powershell
$env:RUNWAY_DATA_DIR = "data/qlob-production/migration-rehearsal-0008"
$env:RUNWAY_PUBLISHING_ENABLED = "false"
$env:RUNWAY_AGENT_RUNTIME = "mock"
.\.venv\Scripts\runway.exe init
.\.venv\Scripts\runway.exe database schema-verify
.\.venv\Scripts\runway.exe database intelligence-doctor --json
```

Return `RUNWAY_DATA_DIR` to `data/qlob-production` only after the rehearsal
passes.

## Apply migration `0008`

With services stopped, a verified backup present, and the safety overrides set:

```powershell
.\.venv\Scripts\runway.exe init
.\.venv\Scripts\runway.exe database schema-verify
.\.venv\Scripts\runway.exe database intelligence-doctor --json
```

Revision `0008_intelligence_data_flywheel` is additive and rollback-aware. It
adds lifecycle tables and provenance fields while preserving `0007` source
evidence. It also normalizes four historical server defaults so clean bootstrap
and incremental upgrade converge exactly.

If schema verification, integrity, foreign keys, or preserved counts differ,
stop. Do not backfill or activate anything.

## Reconcile legacy feedback

```powershell
.\.venv\Scripts\runway.exe intelligence feedback reconcile
.\.venv\Scripts\runway.exe intelligence feedback reconcile
.\.venv\Scripts\runway.exe intelligence feedback verify
```

The second reconciliation must create zero new signals. Verification fails on
missing mappings, target conflicts, duplicate idempotency keys, or channel
leakage. Legacy rows remain preserved.

## Representation lifecycle

### Plan

```powershell
.\.venv\Scripts\runway.exe intelligence representations plan --modality text
.\.venv\Scripts\runway.exe intelligence representations plan --modality image
.\.venv\Scripts\runway.exe intelligence representations plan --modality multimodal
```

Planning freezes exact entity and source hashes. Repeating the same plan returns
the existing set.

### Backfill

```powershell
.\.venv\Scripts\runway.exe intelligence representations backfill `
  --set-id 4 --batch-size 100 --until-complete
```

Backfills are bounded, checkpointed, resumable, idempotent, and inactive. A
source hash change marks the item invalid instead of silently representing new
content under the old plan.

### Validate

```powershell
.\.venv\Scripts\runway.exe intelligence representations verify --set-id 4
```

Validation requires exact item coverage, matching channel/entity/provider/model/
configuration identity, finite vectors, valid normalized-vector norms, and a
consistent vector contract.

### Activate

Create a JSON gate artifact in the ignored production reports directory. Every
value supplied to the CLI must be literal `true`. Trained challenger providers
also require all canonical gates documented in
[`INTELLIGENCE_PROVIDER_MATRIX.md`](INTELLIGENCE_PROVIDER_MATRIX.md).

```powershell
.\.venv\Scripts\runway.exe intelligence representations activate `
  --set-id 4 `
  --reason "Validated replacement after frozen evaluation" `
  --gate-results data/qlob-production/reports/representation-gates.json `
  --yes
```

Activation is transactional. The previous active set becomes `superseded`, the
new set records `supersedes_set_id`, and an `intelligence_activations` row
records the reason and gates.

### Roll back a representation

```powershell
.\.venv\Scripts\runway.exe intelligence representations rollback `
  --set-id 4 `
  --to-set-id 1 `
  --reason "Rollback after measured regression" `
  --yes
```

The target must be the same channel/scope/purpose and remain complete. Rollback
does not delete either set. Do not roll back to a known invalid set merely
because it is the prior version.

## Retrieval cache verification

Run the same safe candidate retrieval twice:

```powershell
.\.venv\Scripts\runway.exe embeddings backfill --limit 2
.\.venv\Scripts\runway.exe embeddings backfill --limit 2
.\.venv\Scripts\runway.exe retrieval inspect --run-id <second-run-id>
```

The second run should identify the same active set IDs and report zero misses,
zero recomputations, and zero stale misses for unchanged inputs. A changed
active set may cause first-pass query-record recomputation; historical active
set records must still be reused.

## Preference datasets and models

Dataset and model targets are separate:

```powershell
.\.venv\Scripts\runway.exe intelligence preference dataset --target caption
.\.venv\Scripts\runway.exe intelligence preference train --dataset-id <id>
.\.venv\Scripts\runway.exe intelligence preference evaluate --model-version-id <id>
```

Repeat with `image` or `pairing` only when that target has real evidence.
Insufficient-data runs are recorded honestly and cannot activate.

Activation requires a JSON artifact with these passing gates:

- `artifact_integrity`;
- `schema_compatibility`;
- `channel_isolation`;
- `quality_non_regression`;
- `calibration_truthful`;
- `offline_only`.

```powershell
.\.venv\Scripts\runway.exe intelligence preference activate `
  --model-version-id <id> `
  --reason "Qualified on frozen creator evidence" `
  --gate-results data/qlob-production/reports/preference-gates.json `
  --yes
```

Rollback restores the persisted parent model and preserves the candidate:

```powershell
.\.venv\Scripts\runway.exe intelligence preference rollback `
  --model-version-id <active-id> `
  --reason "Creator preference regression" `
  --yes
```

Production request scoring loads saved parameters. It never trains or refits.

## Annotation refresh

Planning is safe and makes no model call:

```powershell
.\.venv\Scripts\runway.exe intelligence annotations plan `
  --annotation-version historical-annotation-v3 `
  --prompt-version history-v3
```

Backfill defaults to refusing real model use. A real refresh requires an
explicit operator decision and consumes the configured allowance:

```powershell
.\.venv\Scripts\runway.exe intelligence annotations backfill `
  --run-id <id> --batch-size 5 --allow-model-calls
```

Do not add `--allow-model-calls` to unattended jobs. Verify coverage before any
downstream representation plan:

```powershell
.\.venv\Scripts\runway.exe intelligence annotations verify --run-id <id>
```

Old annotations and raw outputs remain preserved. Incompatible corrections are
reported rather than silently applied.

## Optional trained providers

The production default remains:

```dotenv
RUNWAY_TEXT_EMBEDDING_PROVIDER=runway-local
RUNWAY_MULTIMODAL_EMBEDDING_PROVIDER=runway-local
RUNWAY_EMBEDDING_DEVICE=cpu
```

Optional adapters require an explicitly provisioned trusted local directory and
the optional dependency group:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[intelligence-ml]"
.\.venv\Scripts\runway.exe intelligence provider-status
```

Installing dependencies does not download weights. RunWay uses
`local_files_only=True` and `trust_remote_code=False`. Do not set an adapter as
the configured provider until its exact revision, license, frozen evaluation,
resource measurements, creator study, and rollback rehearsal pass.

## Blind creator study

Follow [`BLIND_CREATOR_STUDY.md`](BLIND_CREATOR_STUDY.md). A study export must
contain at least 50 untouched representative cases before it can support the
requested milestone. Exports hide origin; imports require explicit genuine
reviewer responses and reject duplicates. No synthetic label may be reported as
a creator result.

## Active learning

```powershell
.\.venv\Scripts\runway.exe intelligence active-learning select `
  --target caption --limit 20 --seed 20260718
.\.venv\Scripts\runway.exe intelligence active-learning export `
  --batch-id <id> --output data/qlob-production/reports/active-learning.json
```

Selection is deterministic, uncertainty-first, diversity-aware, target-specific,
and group/split protected. Active-learning cases are not final holdout cases.

## Intelligence doctor

Human-readable:

```powershell
.\.venv\Scripts\runway.exe database intelligence-doctor
```

Machine-readable:

```powershell
.\.venv\Scripts\runway.exe database intelligence-doctor --json
```

For a faster diagnostic that intentionally skips active media-file hashes:

```powershell
.\.venv\Scripts\runway.exe database intelligence-doctor --skip-media-files
```

Do not use the fast form as the final production check.

The doctor audits:

- SQLite integrity, foreign keys, migration, schema fingerprint, WAL, and the
  publishing flag;
- channel isolation across representations, retrieval, captions, preferences,
  feedback, generation, studies, and models;
- conflicting/partial active sets, plan coverage, hashes, vector contracts,
  invalid vectors, and source files;
- annotation coverage and correction compatibility;
- retrieval provenance, selected evidence, active-set identities, and cache
  diagnostics;
- caption slate/candidate/exposure/proposal consistency;
- duplicate labels, immutable feature snapshots, split leakage, target
  separation, and calibration truthfulness;
- legacy-feedback reconciliation and idempotency;
- agent terminal state, deterministic steps, budgets, fallback evidence, and
  publishing capability isolation;
- generation eligibility, rights/safety lineage, and review re-entry;
- experiment artifacts, activation references, and holdout reuse.

Severity meanings:

- `critical`: invariant breach; command exits nonzero and activation/operation
  must stop;
- `warning`: preserved inactive evidence, incomplete optional evidence, or an
  operational risk that does not affect active reads;
- `information`: healthy measurement or intentionally non-set read-through
  cache state.

## Database restoration

Use restoration only for schema/data failure, not ordinary representation or
model regression.

1. Stop API, web, and every process that may hold the SQLite file.
2. Preserve the failed database and any WAL/SHM files under a timestamped
   incident directory.
3. Verify the chosen backup hash and `PRAGMA integrity_check`.
4. Replace the database only while all writers are stopped.
5. Remove stale WAL/SHM companions only after their absolute paths are verified
   to belong to the same stopped database.
6. Start with publishing forced false.
7. Run `schema-status`, `schema-verify` when applicable, the full doctor, and
   preserved row-count checks before ordinary use.

For a model or representation regression, use the transactional lifecycle
rollback commands instead of restoring the entire database.

## Current production checkpoint

The 2026-07-19 Qlob checkpoint is:

- migration `0008_intelligence_data_flywheel`;
- schema fingerprint
  `503e7471206e1465c01ad2bbea9f7a6ee3aa9b0446c7aee6f0e8b4ca4fa4a06d`;
- active representation sets: text `4`, image `2`, multimodal `5`;
- active coverage: `698/698`, `716/716`, `698/698`;
- normalized feedback: 27 signals from 9 preserved legacy rows;
- active preference models: none;
- doctor: 0 critical, 1 warning, 2 information;
- warning: three invalid deterministic text-v1 vectors remain only in
  superseded set `1`;
- backup:
  `data/qlob-production/backups/runway-pre-0008-20260719T034751Z.db`;
- backup SHA-256:
  `9cafe2da9ea937d6b0f5e9dd4509b89fa85d77c6b06c5ea1e47bcd43f0b91d15`.

The warning is intentionally retained audit evidence. Active text-v2 and
multimodal-v2 sets contain no invalid vectors.
