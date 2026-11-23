"""Online SQLite backups taken before `runway db upgrade` and `db downgrade`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from runway.config import Settings
from runway.db.backup import backup_database, backup_directory, prune_automatic_backups


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path / "da#ta 100%")
    settings.ensure_directories()
    return settings


def _seed(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany("INSERT INTO items (name) VALUES (?)", [("a",), ("b",)])
    connection.close()


def test_a_backup_is_a_consistent_copy_including_uncheckpointed_wal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed(settings.database_path)
    # A writer still holds the database open with changes only in the WAL.
    writer = sqlite3.connect(settings.database_path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO items (name) VALUES ('c')")
    writer.commit()
    try:
        backup = backup_database(settings, "before upgrade/0013")
    finally:
        writer.close()

    assert backup.parent == backup_directory(settings)
    assert backup.name.startswith("runway-") and backup.name.endswith("-before-upgrade-0013.db")
    with sqlite3.connect(backup) as copy:
        assert copy.execute("SELECT name FROM items ORDER BY id").fetchall() == [
            ("a",),
            ("b",),
            ("c",),
        ]
    copy.close()
    assert not list(backup.parent.glob("*.partial"))


def test_a_missing_database_is_an_error_not_an_empty_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(FileNotFoundError):
        backup_database(settings, "before-upgrade")
    assert not settings.database_path.exists()


def test_pruning_keeps_the_newest_automatic_backups_and_every_manual_one(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    directory = backup_directory(settings)
    directory.mkdir(parents=True)
    names = [f"runway-2026010{day}T000000000000Z-before-upgrade.db" for day in range(1, 6)]
    for name in [*names, "runway-20260101T000000000000Z-manual.db"]:
        (directory / name).write_bytes(b"")

    removed = prune_automatic_backups(settings, "before-upgrade", 3)

    assert sorted(path.name for path in removed) == names[:2]
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        [*names[2:], "runway-20260101T000000000000Z-manual.db"]
    )


def test_a_backup_that_fails_midway_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _seed(settings.database_path)
    real_connect = sqlite3.connect

    class FailingSource:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def backup(self, target: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            self._connection.close()

    def connect(database: object, *args: object, **kwargs: object) -> object:
        connection = real_connect(database, *args, **kwargs)  # type: ignore[call-overload]
        return FailingSource(connection) if kwargs.get("uri") else connection

    monkeypatch.setattr("runway.db.backup.sqlite3.connect", connect)

    with pytest.raises(sqlite3.OperationalError):
        backup_database(settings, "pre-upgrade")

    assert list(backup_directory(settings).iterdir()) == []
