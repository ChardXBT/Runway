from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DATABASE_ARCHIVE_NAME = "runway-database.db.gz"
MEDIA_ARCHIVE_NAME = "runway-operational-media.zip"
MANIFEST_NAME = "runway-database-manifest.json"


class DatabaseReleaseError(RuntimeError):
    """A private database release failed an integrity or safety contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _database_metadata(path: Path) -> dict[str, Any]:
    with closing(_readonly_connection(path)) as snapshot:
        integrity = str(snapshot.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = snapshot.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok":
            raise DatabaseReleaseError(f"SQLite integrity_check failed: {integrity}")
        if foreign_key_violations:
            raise DatabaseReleaseError(
                f"SQLite foreign_key_check found {len(foreign_key_violations)} violation(s)"
            )
        tables = [
            str(row[0])
            for row in snapshot.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        row_counts: dict[str, int] = {}
        for table in tables:
            quoted = table.replace('"', '""')
            row_counts[table] = int(
                snapshot.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            )
        migration = None
        if "alembic_version" in tables:
            row = snapshot.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            migration = str(row[0]) if row else None
    return {
        "sqlite_integrity_check": integrity,
        "foreign_key_violations": 0,
        "migration": migration,
        "table_count": len(tables),
        "row_counts": row_counts,
    }


def _safe_media_path(data_root: Path, value: str) -> tuple[str, Path]:
    normalized = value.replace("\\", "/").strip()
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "media"
    ):
        raise DatabaseReleaseError(f"unsafe database media path: {value!r}")
    resolved_root = data_root.resolve()
    resolved = (resolved_root / Path(*relative.parts)).resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise DatabaseReleaseError(f"database media escapes the data root: {value!r}")
    return relative.as_posix(), resolved


def _database_media_entries(
    database_path: Path,
    data_root: Path,
) -> list[dict[str, Any]]:
    with closing(_readonly_connection(database_path)) as snapshot:
        media_rows = snapshot.execute(
            """
            SELECT id, local_path, sha256, file_size
            FROM media_assets
            ORDER BY local_path, id
            """
        ).fetchall()

    entries_by_path: dict[str, dict[str, Any]] = {}
    for media_id, local_path, expected_sha256, expected_size in media_rows:
        archive_path, source = _safe_media_path(data_root, str(local_path))
        if not source.is_file():
            raise DatabaseReleaseError(
                f"database media asset {media_id} is missing: {archive_path}"
            )
        actual_size = source.stat().st_size
        if actual_size != int(expected_size):
            raise DatabaseReleaseError(f"database media asset {media_id} has the wrong size")
        actual_sha256 = sha256_file(source)
        if actual_sha256 != str(expected_sha256):
            raise DatabaseReleaseError(f"database media asset {media_id} failed its SHA-256 check")
        existing = entries_by_path.get(archive_path)
        if existing is not None:
            if existing["sha256"] != actual_sha256 or existing["bytes"] != actual_size:
                raise DatabaseReleaseError(f"two database media assets conflict at {archive_path}")
            existing_ids = existing["media_asset_ids"]
            if not isinstance(existing_ids, list):
                raise DatabaseReleaseError("database media manifest assembly failed")
            existing_ids.append(int(media_id))
            continue
        entries_by_path[archive_path] = {
            "media_asset_ids": [int(media_id)],
            "path": archive_path,
            "bytes": actual_size,
            "sha256": actual_sha256,
            "source_path": str(source),
        }
    return list(entries_by_path.values())


def _create_media_archive(path: Path, entries: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        written: set[str] = set()
        for entry in sorted(entries, key=lambda value: str(value["path"])):
            archive_path = str(entry["path"])
            if archive_path in written:
                continue
            written.add(archive_path)
            info = zipfile.ZipInfo(archive_path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            info.create_system = 3
            source = Path(str(entry["source_path"]))
            archive.writestr(info, source.read_bytes())


def _gzip_database(source: Path, destination: Path) -> None:
    with (
        source.open("rb") as source_handle,
        destination.open("wb") as raw_target,
        gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_target,
            compresslevel=9,
            mtime=0,
        ) as target_handle,
    ):
        shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)


def create_release(
    *,
    source_database: Path,
    data_root: Path,
    output_directory: Path,
    commit_sha: str,
    branch: str,
) -> dict[str, Any]:
    source_database = source_database.resolve()
    data_root = data_root.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    backup = output_directory / "runway-database.db"
    database_archive = output_directory / DATABASE_ARCHIVE_NAME
    media_archive = output_directory / MEDIA_ARCHIVE_NAME
    manifest_path = output_directory / MANIFEST_NAME

    with (
        closing(_readonly_connection(source_database)) as reader,
        closing(sqlite3.connect(backup)) as writer,
    ):
        reader.backup(writer)
    metadata = _database_metadata(backup)
    media_entries = _database_media_entries(backup, data_root)
    _gzip_database(backup, database_archive)
    _create_media_archive(media_archive, media_entries)

    public_media_entries = [
        {key: value for key, value in entry.items() if key != "source_path"}
        for entry in media_entries
    ]
    manifest: dict[str, Any] = {
        "format_version": 3,
        "repository": "ChardXBT/Runway",
        "source": "data/qlob-production/runway.db",
        "commit_sha": commit_sha.casefold(),
        "branch": branch,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "database_bytes": backup.stat().st_size,
        "database_sha256": sha256_file(backup),
        "archive_name": DATABASE_ARCHIVE_NAME,
        "archive_bytes": database_archive.stat().st_size,
        "archive_sha256": sha256_file(database_archive),
        "compression": "gzip-9",
        **metadata,
        "database_media": {
            "archive_name": MEDIA_ARCHIVE_NAME,
            "archive_bytes": media_archive.stat().st_size,
            "archive_sha256": sha256_file(media_archive),
            "entry_count": len(public_media_entries),
            "media_asset_count": sum(
                len(entry["media_asset_ids"]) for entry in public_media_entries
            ),
            "content_bytes": sum(int(entry["bytes"]) for entry in public_media_entries),
            "entries": public_media_entries,
        },
        "excluded": [
            "browser profiles and authentication state",
            "SQLite WAL/SHM files",
            "media files not referenced by the canonical database",
            "logs, reports, backups, and migration rehearsals",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verify_release(
        manifest_path=manifest_path,
        database_archive=database_archive,
        media_archive=media_archive,
    )
    return manifest


def _validate_archive(path: Path, *, size: int, sha256: str, label: str) -> None:
    if not path.is_file():
        raise DatabaseReleaseError(f"{label} archive is missing")
    if path.stat().st_size != size:
        raise DatabaseReleaseError(f"{label} archive has the wrong size")
    if sha256_file(path) != sha256:
        raise DatabaseReleaseError(f"{label} archive failed its SHA-256 check")


def verify_release(
    *,
    manifest_path: Path,
    database_archive: Path,
    media_archive: Path,
    output_directory: Path | None = None,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != 3:
        raise DatabaseReleaseError("unsupported database release manifest version")
    _validate_archive(
        database_archive,
        size=int(manifest["archive_bytes"]),
        sha256=str(manifest["archive_sha256"]),
        label="database",
    )
    media = manifest["database_media"]
    _validate_archive(
        media_archive,
        size=int(media["archive_bytes"]),
        sha256=str(media["archive_sha256"]),
        label="database media",
    )

    with tempfile.TemporaryDirectory(prefix="runway-release-verify-") as temporary:
        restored_database = Path(temporary) / "runway.db"
        with gzip.open(database_archive, "rb") as source, restored_database.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if restored_database.stat().st_size != int(manifest["database_bytes"]):
            raise DatabaseReleaseError("restored database has the wrong size")
        if sha256_file(restored_database) != str(manifest["database_sha256"]):
            raise DatabaseReleaseError("restored database failed its SHA-256 check")
        metadata = _database_metadata(restored_database)
        for key in (
            "sqlite_integrity_check",
            "foreign_key_violations",
            "migration",
            "table_count",
            "row_counts",
        ):
            if metadata[key] != manifest[key]:
                raise DatabaseReleaseError(f"restored database {key} does not match manifest")

        expected_entries = {str(entry["path"]): entry for entry in media["entries"]}
        if len(expected_entries) != int(media["entry_count"]):
            raise DatabaseReleaseError("database media manifest contains duplicate paths")
        if sum(len(entry["media_asset_ids"]) for entry in media["entries"]) != int(
            media["media_asset_count"]
        ):
            raise DatabaseReleaseError("database media asset count does not match manifest")
        with zipfile.ZipFile(media_archive) as archive:
            archive_paths = [info.filename for info in archive.infolist() if not info.is_dir()]
            if len(archive_paths) != len(set(archive_paths)):
                raise DatabaseReleaseError("database media archive contains duplicate paths")
            if set(archive_paths) != set(expected_entries):
                raise DatabaseReleaseError("database media archive contents do not match manifest")
            for archive_path in archive_paths:
                normalized, _unused = _safe_media_path(Path(temporary), archive_path)
                if normalized != archive_path:
                    raise DatabaseReleaseError("database media path is not canonical")
                payload = archive.read(archive_path)
                entry = expected_entries[archive_path]
                if len(payload) != int(entry["bytes"]):
                    raise DatabaseReleaseError(f"database media has the wrong size: {archive_path}")
                if hashlib.sha256(payload).hexdigest() != str(entry["sha256"]):
                    raise DatabaseReleaseError(
                        f"database media failed its SHA-256 check: {archive_path}"
                    )

            if output_directory is not None:
                output_directory = output_directory.resolve()
                output_directory.mkdir(parents=True, exist_ok=True)
                destination_database = output_directory / "runway.db"
                if destination_database.exists():
                    raise DatabaseReleaseError(
                        f"restore destination already contains {destination_database.name}"
                    )
                destinations: list[tuple[str, Path]] = []
                for archive_path in archive_paths:
                    _normalized, destination = _safe_media_path(
                        output_directory,
                        archive_path,
                    )
                    if destination.exists():
                        raise DatabaseReleaseError(
                            f"restore destination already contains {archive_path}"
                        )
                    destinations.append((archive_path, destination))

                # Preflight every destination before writing anything so a stale
                # media file cannot leave a deceptively partial restore behind.
                shutil.copy2(restored_database, destination_database)
                for archive_path, destination in destinations:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(archive_path))

    return {
        "status": "verified",
        "commit_sha": manifest["commit_sha"],
        "migration": manifest["migration"],
        "table_count": manifest["table_count"],
        "database_media_entries": media["entry_count"],
        "database_media_assets": media["media_asset_count"],
        "restored_to": str(output_directory) if output_directory is not None else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify Runway's private database release.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--source-database", type=Path, required=True)
    create.add_argument("--data-root", type=Path, required=True)
    create.add_argument("--output-directory", type=Path, required=True)
    create.add_argument("--commit-sha", required=True)
    create.add_argument("--branch", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--database-archive", type=Path, required=True)
    verify.add_argument("--media-archive", type=Path, required=True)
    verify.add_argument("--output-directory", type=Path)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.command == "create":
        result = create_release(
            source_database=arguments.source_database,
            data_root=arguments.data_root,
            output_directory=arguments.output_directory,
            commit_sha=arguments.commit_sha,
            branch=arguments.branch,
        )
    else:
        result = verify_release(
            manifest_path=arguments.manifest,
            database_archive=arguments.database_archive,
            media_archive=arguments.media_archive,
            output_directory=arguments.output_directory,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
