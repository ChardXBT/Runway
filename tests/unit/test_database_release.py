from __future__ import annotations

import hashlib
import sqlite3
import zipfile
from pathlib import Path

import pytest

from runway.operations.database_release import (
    DATABASE_ARCHIVE_NAME,
    MANIFEST_NAME,
    MEDIA_ARCHIVE_NAME,
    DatabaseReleaseError,
    create_release,
    verify_release,
)


def _release_fixture(root: Path) -> tuple[Path, Path]:
    data_root = root / "data"
    media_root = data_root / "media" / "candidate"
    media_root.mkdir(parents=True)
    active = media_root / "active.jpg"
    preview = media_root / "active-preview.jpg"
    rejected = media_root / "rejected.jpg"
    active.write_bytes(b"active-image")
    preview.write_bytes(b"active-preview")
    rejected.write_bytes(b"rejected-image")

    database = data_root / "runway.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            CREATE TABLE media_assets (
                id INTEGER PRIMARY KEY,
                local_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL
            );
            CREATE TABLE candidate_images (
                id INTEGER PRIMARY KEY,
                media_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
                preview_asset_id INTEGER REFERENCES media_assets(id)
            );
            CREATE TABLE proposals (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                candidate_image_id INTEGER NOT NULL REFERENCES candidate_images(id),
                backup_candidate_ids_json TEXT NOT NULL
            );
            INSERT INTO alembic_version VALUES ('0010_neural_intelligence');
            """
        )
        for media_id, path in enumerate((active, preview, rejected), start=1):
            connection.execute(
                "INSERT INTO media_assets VALUES (?, ?, ?, ?)",
                (
                    media_id,
                    path.relative_to(data_root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_size,
                ),
            )
        connection.execute("INSERT INTO candidate_images VALUES (1, 1, 2)")
        connection.execute("INSERT INTO candidate_images VALUES (2, 3, NULL)")
        connection.execute("INSERT INTO proposals VALUES (1, 'needs_review', 1, '[]')")
        connection.execute("INSERT INTO proposals VALUES (2, 'rejected', 2, '[]')")
    return data_root, database


def test_private_database_release_restores_database_and_all_referenced_media(
    tmp_path: Path,
) -> None:
    data_root, database = _release_fixture(tmp_path)
    release = tmp_path / "release"
    manifest = create_release(
        source_database=database,
        data_root=data_root,
        output_directory=release,
        commit_sha="a" * 40,
        branch="main",
    )

    assert manifest["format_version"] == 3
    assert manifest["database_media"]["entry_count"] == 3
    assert manifest["database_media"]["media_asset_count"] == 3
    assert {entry["path"] for entry in manifest["database_media"]["entries"]} == {
        "media/candidate/active.jpg",
        "media/candidate/active-preview.jpg",
        "media/candidate/rejected.jpg",
    }
    with zipfile.ZipFile(release / MEDIA_ARCHIVE_NAME) as archive:
        assert "media/candidate/rejected.jpg" in archive.namelist()

    restored = tmp_path / "restored"
    result = verify_release(
        manifest_path=release / MANIFEST_NAME,
        database_archive=release / DATABASE_ARCHIVE_NAME,
        media_archive=release / MEDIA_ARCHIVE_NAME,
        output_directory=restored,
    )

    assert result["status"] == "verified"
    assert (restored / "runway.db").is_file()
    assert (restored / "media" / "candidate" / "active.jpg").read_bytes() == (b"active-image")
    assert (restored / "media" / "candidate" / "rejected.jpg").read_bytes() == (b"rejected-image")


def test_private_database_release_rejects_tampered_media_archive(tmp_path: Path) -> None:
    data_root, database = _release_fixture(tmp_path)
    release = tmp_path / "release"
    create_release(
        source_database=database,
        data_root=data_root,
        output_directory=release,
        commit_sha="b" * 40,
        branch="main",
    )
    with (release / MEDIA_ARCHIVE_NAME).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(DatabaseReleaseError, match="wrong size"):
        verify_release(
            manifest_path=release / MANIFEST_NAME,
            database_archive=release / DATABASE_ARCHIVE_NAME,
            media_archive=release / MEDIA_ARCHIVE_NAME,
        )


def test_private_database_release_rejects_media_path_escape(tmp_path: Path) -> None:
    data_root, database = _release_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE media_assets SET local_path = '../secret.jpg' WHERE id = 1")

    with pytest.raises(DatabaseReleaseError, match="unsafe database media path"):
        create_release(
            source_database=database,
            data_root=data_root,
            output_directory=tmp_path / "release",
            commit_sha="c" * 40,
            branch="main",
        )


def test_private_database_release_preflights_restore_conflicts(tmp_path: Path) -> None:
    data_root, database = _release_fixture(tmp_path)
    release = tmp_path / "release"
    create_release(
        source_database=database,
        data_root=data_root,
        output_directory=release,
        commit_sha="d" * 40,
        branch="main",
    )
    restored = tmp_path / "restored"
    conflict = restored / "media" / "candidate" / "rejected.jpg"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"keep-me")

    with pytest.raises(DatabaseReleaseError, match="already contains"):
        verify_release(
            manifest_path=release / MANIFEST_NAME,
            database_archive=release / DATABASE_ARCHIVE_NAME,
            media_archive=release / MEDIA_ARCHIVE_NAME,
            output_directory=restored,
        )

    assert not (restored / "runway.db").exists()
    assert conflict.read_bytes() == b"keep-me"
