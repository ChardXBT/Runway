from __future__ import annotations

import copy
import json
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command
from runway.config import Settings
from runway.db.schema_contract import (
    compare_schema_snapshots,
    schema_fingerprint,
    schema_snapshot,
)


def _config(settings: Settings) -> Config:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(settings.project_root / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def _migrate(settings: Settings, revision: str) -> None:
    settings.ensure_directories()
    command.upgrade(_config(settings), revision)


def test_clean_and_existing_0007_upgrade_have_identical_schema(
    tmp_path: Path,
) -> None:
    clean = Settings(data_dir=tmp_path / "clean")
    _migrate(clean, "head")
    clean_engine = create_engine(clean.database_url)
    clean_snapshot = schema_snapshot(clean_engine)
    clean_engine.dispose()

    upgraded = Settings(data_dir=tmp_path / "upgraded")
    _migrate(upgraded, "head")
    command.downgrade(_config(upgraded), "0007_canonical_intelligence")
    command.upgrade(_config(upgraded), "head")
    upgraded_engine = create_engine(upgraded.database_url)
    upgraded_snapshot = schema_snapshot(upgraded_engine)
    upgraded_engine.dispose()

    assert clean_snapshot["fingerprint"] == upgraded_snapshot["fingerprint"]
    assert clean_snapshot["contract"] == upgraded_snapshot["contract"]


def test_schema_fingerprint_is_deterministic(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "deterministic")
    _migrate(settings, "head")
    engine = create_engine(settings.database_url)
    first = schema_snapshot(engine)
    second = schema_snapshot(engine)
    engine.dispose()

    assert first == second
    assert compare_schema_snapshots(first, second)["matches"] is True


def test_schema_matches_committed_contract_snapshot(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "contract")
    _migrate(settings, "head")
    engine = create_engine(settings.database_url)
    observed = schema_snapshot(engine)
    engine.dispose()

    snapshot_path = settings.project_root / "docs" / "schema" / "intelligence-data-flywheel.json"
    expected: object = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert isinstance(expected, dict)
    assert observed["migration"] == expected["migration"]
    assert observed["fingerprint"] == expected["fingerprint"]
    contract = observed["contract"]
    assert isinstance(contract, dict)
    tables = contract["tables"]
    assert isinstance(tables, dict)
    assert len(tables) == expected["table_count"]


def test_schema_comparison_identifies_missing_index_and_foreign_key(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "structural-diff")
    _migrate(settings, "head")
    engine = create_engine(settings.database_url)
    expected = schema_snapshot(engine)
    engine.dispose()
    observed = copy.deepcopy(expected)
    contract = observed["contract"]
    assert isinstance(contract, dict)
    tables = contract["tables"]
    assert isinstance(tables, dict)

    representation_sets = tables["representation_sets"]
    assert isinstance(representation_sets, dict)
    indexes = representation_sets["indexes"]
    assert isinstance(indexes, list)
    removed_index = indexes.pop()

    representation_items = tables["representation_set_items"]
    assert isinstance(representation_items, dict)
    foreign_keys = representation_items["foreign_keys"]
    assert isinstance(foreign_keys, list)
    removed_foreign_key = foreign_keys.pop()

    observed["fingerprint"] = schema_fingerprint(contract)
    comparison = compare_schema_snapshots(expected, observed)
    assert comparison["matches"] is False
    differences = comparison["differences"]
    assert isinstance(differences, dict)
    table_differences = differences["tables"]
    assert isinstance(table_differences, dict)
    set_differences = table_differences["representation_sets"]
    assert isinstance(set_differences, dict)
    assert removed_index in set_differences["missing_indexes"]
    item_differences = table_differences["representation_set_items"]
    assert isinstance(item_differences, dict)
    assert removed_foreign_key in item_differences["missing_foreign_keys"]


def test_schema_comparison_identifies_changed_column_attributes(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "changed-column")
    _migrate(settings, "head")
    engine = create_engine(settings.database_url)
    expected = schema_snapshot(engine)
    engine.dispose()
    observed = copy.deepcopy(expected)
    contract = observed["contract"]
    assert isinstance(contract, dict)
    tables = contract["tables"]
    assert isinstance(tables, dict)
    proposals = tables["proposals"]
    assert isinstance(proposals, dict)
    columns = proposals["columns"]
    assert isinstance(columns, list)
    status = next(
        column
        for column in columns
        if isinstance(column, dict) and column.get("name") == "status"
    )
    assert isinstance(status, dict)
    status["nullable"] = not bool(status["nullable"])
    observed["fingerprint"] = schema_fingerprint(contract)

    comparison = compare_schema_snapshots(expected, observed)
    differences = comparison["differences"]
    assert isinstance(differences, dict)
    table_differences = differences["tables"]
    assert isinstance(table_differences, dict)
    proposal_differences = table_differences["proposals"]
    assert isinstance(proposal_differences, dict)
    changed = proposal_differences["changed_columns"]
    assert isinstance(changed, list)
    assert changed[0]["expected"]["name"] == "status"
