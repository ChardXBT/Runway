from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, inspect, text

SCHEMA_CONTRACT_VERSION = "runway-schema-contract-v1"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _normalized_default(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def normalized_schema(engine: Engine) -> dict[str, object]:
    """Return a deterministic structural contract for the current database."""
    inspector = inspect(engine)
    tables: dict[str, object] = {}
    for table in sorted(inspector.get_table_names()):
        columns = sorted(
            (
                {
                    "name": str(column["name"]),
                    "type": str(column["type"]).upper(),
                    "nullable": bool(column.get("nullable", True)),
                    "default": _normalized_default(column.get("default")),
                    "primary_key": int(str(column.get("primary_key", 0) or 0)),
                }
                for column in inspector.get_columns(table)
            ),
            key=lambda row: str(row["name"]),
        )
        foreign_keys = sorted(
            (
                {
                    "columns": _string_list(foreign_key.get("constrained_columns")),
                    "referred_table": str(foreign_key.get("referred_table") or ""),
                    "referred_columns": _string_list(foreign_key.get("referred_columns")),
                    "ondelete": (
                        str(options["ondelete"])
                        if isinstance(
                            options := foreign_key.get("options"),
                            dict,
                        )
                        and options.get("ondelete") is not None
                        else None
                    ),
                }
                for foreign_key in inspector.get_foreign_keys(table)
            ),
            key=lambda row: (
                row["columns"],
                row["referred_table"],
                row["referred_columns"],
            ),
        )
        indexes = sorted(
            (
                {
                    "name": str(index.get("name") or ""),
                    "columns": _string_list(index.get("column_names")),
                    "unique": bool(index.get("unique", False)),
                }
                for index in inspector.get_indexes(table)
            ),
            key=lambda row: (row["name"], row["columns"]),
        )
        unique_constraints = sorted(
            (
                {
                    "name": str(constraint.get("name") or ""),
                    "columns": _string_list(constraint.get("column_names")),
                }
                for constraint in inspector.get_unique_constraints(table)
            ),
            key=lambda row: (row["name"], row["columns"]),
        )
        check_constraints = sorted(
            (
                {
                    "name": str(constraint.get("name") or ""),
                    "sqltext": " ".join(str(constraint.get("sqltext") or "").split()),
                }
                for constraint in inspector.get_check_constraints(table)
            ),
            key=lambda row: (row["name"], row["sqltext"]),
        )
        tables[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
            "unique_constraints": unique_constraints,
            "check_constraints": check_constraints,
        }
    return {
        "contract_version": SCHEMA_CONTRACT_VERSION,
        "tables": tables,
    }


def schema_fingerprint(contract: dict[str, object]) -> str:
    encoded = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_migration(engine: Engine) -> str | None:
    with engine.connect() as connection:
        row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
    return str(row[0]) if row else None


def schema_snapshot(engine: Engine) -> dict[str, object]:
    contract = normalized_schema(engine)
    tables = contract["tables"]
    if not isinstance(tables, dict):
        raise RuntimeError("normalized schema contract has no table mapping")
    return {
        "contract_version": SCHEMA_CONTRACT_VERSION,
        "migration": current_migration(engine),
        "fingerprint": schema_fingerprint(contract),
        "table_count": len(tables),
        "contract": contract,
    }


def write_schema_snapshot(engine: Engine, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(schema_snapshot(engine), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def load_schema_snapshot(path: Path) -> dict[str, Any]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not contain a schema snapshot object")
    return payload


def compare_schema_snapshots(
    expected: dict[str, object],
    observed: dict[str, object],
) -> dict[str, object]:
    expected_fingerprint = str(expected.get("fingerprint") or "")
    observed_fingerprint = str(observed.get("fingerprint") or "")
    differences = _schema_differences(expected, observed)
    return {
        "matches": (
            expected_fingerprint == observed_fingerprint
            and not differences["missing_tables"]
            and not differences["unexpected_tables"]
            and not differences["tables"]
        ),
        "expected_migration": expected.get("migration"),
        "observed_migration": observed.get("migration"),
        "expected_fingerprint": expected_fingerprint,
        "observed_fingerprint": observed_fingerprint,
        "differences": differences,
    }


def _schema_differences(
    expected: dict[str, object],
    observed: dict[str, object],
) -> dict[str, object]:
    expected_tables = _snapshot_tables(expected)
    observed_tables = _snapshot_tables(observed)
    shared_tables = sorted(expected_tables.keys() & observed_tables.keys())
    table_differences: dict[str, object] = {}
    for table in shared_tables:
        expected_table = expected_tables[table]
        observed_table = observed_tables[table]
        differences: dict[str, object] = {}
        for collection, identity_fields in (
            ("columns", ("name",)),
            ("indexes", ("name", "columns")),
            (
                "foreign_keys",
                ("columns", "referred_table", "referred_columns", "ondelete"),
            ),
            ("unique_constraints", ("name", "columns")),
            ("check_constraints", ("name", "sqltext")),
        ):
            expected_rows = _contract_rows(expected_table, collection)
            observed_rows = _contract_rows(observed_table, collection)
            expected_by_identity = {
                _row_identity(row, identity_fields): row for row in expected_rows
            }
            observed_by_identity = {
                _row_identity(row, identity_fields): row for row in observed_rows
            }
            missing = [
                expected_by_identity[key]
                for key in sorted(expected_by_identity.keys() - observed_by_identity.keys())
            ]
            unexpected = [
                observed_by_identity[key]
                for key in sorted(observed_by_identity.keys() - expected_by_identity.keys())
            ]
            changed = [
                {
                    "expected": expected_by_identity[key],
                    "observed": observed_by_identity[key],
                }
                for key in sorted(expected_by_identity.keys() & observed_by_identity.keys())
                if expected_by_identity[key] != observed_by_identity[key]
            ]
            if missing:
                differences[f"missing_{collection}"] = missing
            if unexpected:
                differences[f"unexpected_{collection}"] = unexpected
            if changed:
                differences[f"changed_{collection}"] = changed
        if differences:
            table_differences[table] = differences
    return {
        "missing_tables": sorted(expected_tables.keys() - observed_tables.keys()),
        "unexpected_tables": sorted(observed_tables.keys() - expected_tables.keys()),
        "tables": table_differences,
    }


def _snapshot_tables(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    contract = snapshot.get("contract")
    if not isinstance(contract, dict):
        return {}
    tables = contract.get("tables")
    if not isinstance(tables, dict):
        return {}
    return {str(name): value for name, value in tables.items() if isinstance(value, dict)}


def _contract_rows(
    table: dict[str, object],
    collection: str,
) -> list[dict[str, object]]:
    value = table.get(collection)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _row_identity(
    row: dict[str, object],
    fields: tuple[str, ...],
) -> str:
    return json.dumps(
        [row.get(field) for field in fields],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
