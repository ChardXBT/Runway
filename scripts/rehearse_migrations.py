#!/usr/bin/env python3
"""Rehearse Runway's migration upgrade/downgrade on disposable SQLite copies.

The source database is opened read-only and copied with SQLite's online backup API.
Only the scratch copies are migrated. The script never edits the source and never
removes pre-existing scratch files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

DEFAULT_DOWNGRADE_TARGET = "0012_creator_platforms"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def backup_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".db.partial")
    source_connection = _connect_readonly(source)
    try:
        target_connection = sqlite3.connect(partial)
        try:
            source_connection.backup(target_connection)
            check = target_connection.execute("PRAGMA quick_check").fetchone()
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    if check is None or check[0] != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"copied database failed its integrity check: {check}")
    partial.replace(destination)


def database_stats(path: Path) -> dict[str, Any]:
    connection = _connect_readonly(path)
    try:
        tables = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        counts = {
            table: int(connection.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0])
            for table in tables
        }
        schema = {}
        for table in tables:
            columns = [
                tuple(row) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
            ]
            foreign_keys = sorted(
                tuple(row[1:])
                for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})")
            )
            indexes = []
            for index in connection.execute(f"PRAGMA index_list({_quote(table)})"):
                index_name = index[1]
                index_columns = tuple(
                    row[2] for row in connection.execute(f"PRAGMA index_info({_quote(index_name)})")
                )
                indexes.append((tuple(index[1:]), index_columns))
            schema[table] = {
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": sorted(indexes),
            }
        row_hashes = {}
        for table in tables:
            canonical_rows = sorted(
                repr(row) for row in connection.execute(f"SELECT * FROM {_quote(table)}")
            )
            row_hashes[table] = hashlib.sha256("\n".join(canonical_rows).encode()).hexdigest()
        revision = None
        if "alembic_version" in tables:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_rows = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        return {
            "path": str(path),
            "revision": revision,
            "table_count": len(tables),
            "tables": tables,
            "counts": counts,
            "schema": schema,
            "row_total": sum(counts.values()),
            "row_hashes": row_hashes,
            "integrity_check": integrity,
            "foreign_key_violation_count": len(foreign_key_rows),
            "foreign_key_violations_preview": foreign_key_rows[:20],
        }
    finally:
        connection.close()


def value_differences(
    before: dict[str, object], after: dict[str, object], tables: set[str]
) -> dict[str, dict[str, object]]:
    differences: dict[str, dict[str, object]] = {}
    for table in sorted(tables):
        old = before.get(table)
        new = after.get(table)
        if old != new:
            differences[table] = {"before": old, "after": new}
    return differences


def count_differences(
    before: dict[str, int], after: dict[str, int], tables: set[str]
) -> dict[str, dict[str, int | None]]:
    differences: dict[str, dict[str, int | None]] = {}
    for table in sorted(tables):
        old = before.get(table)
        new = after.get(table)
        if old != new:
            differences[table] = {"before": old, "after": new}
    return differences


def candidate_python(candidate: Path) -> Path:
    executable = candidate / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise ValueError(f"candidate virtualenv Python not found: {executable}")
    return executable


def run_migration(candidate: Path, python: Path, data_dir: Path, *args: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["RUNWAY_DATA_DIR"] = str(data_dir)
    env["RUNWAY_INSTANCE_DIR"] = str(data_dir / "instance")
    completed = subprocess.run(
        [str(python), "-m", "runway.cli.db_commands", *args],
        cwd=candidate,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {
        "command": " ".join(args),
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def candidate_migration_head(candidate: Path) -> str:
    config = Config()
    config.set_main_option("script_location", str(candidate / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise ValueError(f"no Alembic head found in {candidate}")
    return head


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, type=Path, help="source runway.db, opened read-only"
    )
    parser.add_argument(
        "--candidate", required=True, type=Path, help="new release-candidate checkout"
    )
    parser.add_argument(
        "--scratch", required=True, type=Path, help="empty directory outside every repository"
    )
    parser.add_argument("--expected-head", help="default: the candidate Alembic head")
    parser.add_argument("--downgrade-target", default=DEFAULT_DOWNGRADE_TARGET)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    candidate = args.candidate.resolve()
    scratch = args.scratch.resolve()
    if not source.is_file():
        print(f"source database does not exist: {source}", file=sys.stderr)
        return 2
    if not candidate.is_dir():
        print(f"candidate checkout does not exist: {candidate}", file=sys.stderr)
        return 2
    if scratch.exists() and any(scratch.iterdir()):
        print(f"scratch directory is not empty: {scratch}", file=sys.stderr)
        return 2
    try:
        python = candidate_python(candidate)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    expected_head = args.expected_head or candidate_migration_head(candidate)
    scratch.mkdir(parents=True, exist_ok=True)
    run_dir = scratch / f"rehearsal-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    run_dir.mkdir()
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_argument": str(source),
        "candidate": str(candidate),
        "expected_head": expected_head,
        "downgrade_target": args.downgrade_target,
        "passed": False,
        "problems": [],
    }
    try:
        report["source"] = database_stats(source)
        copy_a_db = run_dir / "copy-a" / "data" / "runway.db"
        copy_b_db = run_dir / "copy-b" / "data" / "runway.db"
        backup_copy(source, copy_a_db)
        backup_copy(source, copy_b_db)

        for label, database in (("copy_a", copy_a_db), ("copy_b", copy_b_db)):
            data_dir = database.parent
            upgrade = run_migration(candidate, python, data_dir, "upgrade")
            result: dict[str, Any] = {"upgrade": upgrade}
            if upgrade["returncode"] == 0:
                result.update(database_stats(database))
            else:
                report["problems"].append(f"{label} upgrade exited {upgrade['returncode']}")
            report[label] = result

        if report["copy_b"]["upgrade"]["returncode"] == 0:
            downgrade = run_migration(
                candidate,
                python,
                copy_b_db.parent,
                "downgrade",
                args.downgrade_target,
            )
            report["copy_b"]["downgrade"] = downgrade
            if downgrade["returncode"] != 0:
                report["problems"].append(f"copy_b downgrade exited {downgrade['returncode']}")
            report["copy_b"].update(database_stats(copy_b_db))

        if "counts" in report.get("copy_a", {}):
            before_tables = set(report["source"]["counts"])
            report["copy_a"]["original_count_differences"] = count_differences(
                report["source"]["counts"], report["copy_a"]["counts"], before_tables
            )
            if report["copy_a"]["original_count_differences"]:
                report["problems"].append("copy A changed original-table row counts")
        if "counts" in report.get("copy_b", {}):
            before_tables = set(report["source"]["counts"])
            after_tables = set(report["copy_b"]["counts"])
            report["copy_b"]["missing_tables"] = sorted(before_tables - after_tables)
            report["copy_b"]["extra_tables"] = sorted(after_tables - before_tables)
            shared_tables = before_tables & after_tables
            report["copy_b"]["count_differences"] = count_differences(
                report["source"]["counts"], report["copy_b"]["counts"], shared_tables
            )
            report["copy_b"]["schema_differences"] = value_differences(
                report["source"]["schema"], report["copy_b"]["schema"], shared_tables
            )
            report["copy_b"]["row_differences"] = value_differences(
                report["source"]["row_hashes"], report["copy_b"]["row_hashes"], shared_tables
            )
            if report["copy_b"]["missing_tables"]:
                report["problems"].append("copy B is missing source tables")
            if report["copy_b"]["extra_tables"]:
                report["problems"].append("copy B has extra tables")
            if report["copy_b"]["count_differences"]:
                report["problems"].append("copy B changed row counts")
            if report["copy_b"]["schema_differences"]:
                report["problems"].append("copy B changed table schemas")
            if report["copy_b"]["row_differences"]:
                report["problems"].append("copy B changed row contents")

        for label in ("source", "copy_a", "copy_b"):
            if report.get(label, {}).get("integrity_check") != "ok":
                report["problems"].append(f"{label} integrity check is not ok")
        if report.get("copy_a", {}).get("revision") != expected_head:
            report["problems"].append("copy A did not reach the expected head")
        if report.get("copy_b", {}).get("revision") != args.downgrade_target:
            report["problems"].append("copy B did not reach the downgrade target")
    except Exception as exc:  # report every rehearsal failure; never mutate the source
        report["problems"].append(f"{type(exc).__name__}: {exc}")

    report["passed"] = not report["problems"]
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "problems": report["problems"]}, indent=2))
    print("REHEARSAL PASSED" if report["passed"] else "REHEARSAL FAILED")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
