# Operations

Use `leeway init` once, `leeway doctor` after dependency/configuration changes, and
`run-leeway.ps1` (Windows) or `run-leeway.sh` (POSIX) to run the local application. Capture is not
scheduled. Build a new profile explicitly after catalogue changes, then run discovery and batch
generation.

Routine reports are written to `data/reports/`. Capture failure bundles are in `data/snapshots/`.
The SQLite database uses WAL; copy the database plus `-wal`/`-shm` files only after stopping Leeway,
or use SQLite's backup facility.
