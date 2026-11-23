"""Online SQLite backups taken before schema changes.

Uses SQLite's online backup API (as `docs/INTELLIGENCE_OPERATIONS.md`
describes), so a copy is consistent even while the API or worker is writing.
Copies go to the ignored `data/backups/` directory, never into the repository.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from runway.config import Settings

AUTOMATIC_BACKUPS_KEPT = 3
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def backup_directory(settings: Settings) -> Path:
    return settings.resolved_data_dir / "backups"


def backup_database(settings: Settings, reason: str) -> Path:
    """Copy the configured database to `data/backups/runway-<UTC time>-<reason>.db`."""
    source_path = settings.database_path
    if not source_path.is_file():
        raise FileNotFoundError(f"no database to back up at {source_path}")
    directory = backup_directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"runway-{stamp}-{_SAFE.sub('-', reason).strip('-')}.db"
    partial = destination.with_suffix(".db.partial")
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
            check = target.execute("PRAGMA quick_check").fetchone()
        finally:
            target.close()
    finally:
        source.close()
    if check is None or check[0] != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"the backup failed its integrity check: {check}")
    partial.replace(destination)
    return destination


def prune_automatic_backups(settings: Settings, reason: str, keep: int) -> list[Path]:
    """Keep the newest `keep` backups made for `reason`; others made by hand stay."""
    pattern = f"runway-*-{_SAFE.sub('-', reason).strip('-')}.db"
    found = sorted(backup_directory(settings).glob(pattern), key=lambda path: path.name)
    stale = found[: max(len(found) - keep, 0)]
    for path in stale:
        path.unlink()
    return stale
