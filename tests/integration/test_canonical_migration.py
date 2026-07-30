from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db import initialize_database


def _alembic_config(settings: Settings) -> Config:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(settings.project_root / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def test_existing_schema_upgrade_and_rollback_preserve_catalogue(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "migration-data")
    database = initialize_database(settings)
    CaptureService(database, settings).run_fixture()
    with database.session() as session:
        post_count = session.execute(text("SELECT count(*) FROM posts")).scalar_one()
    assert post_count > 0
    database.engine.dispose()
    config = _alembic_config(settings)

    command.downgrade(config, "0006_uncapped_lineup")
    downgraded_engine = create_engine(settings.database_url)
    downgraded = inspect(downgraded_engine)
    assert "caption_slates" not in downgraded.get_table_names()
    assert "caption_slate_id" not in {
        column["name"] for column in downgraded.get_columns("proposals")
    }
    with downgraded_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM posts")).scalar_one() == post_count
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0006_uncapped_lineup"
        )
    downgraded_engine.dispose()

    command.upgrade(config, "head")
    upgraded_engine = create_engine(settings.database_url)
    upgraded = inspect(upgraded_engine)
    required_tables = {
        "representation_records",
        "intelligence_retrieval_runs",
        "retrieval_evidence_records",
        "caption_slates",
        "caption_candidate_records",
        "caption_exposures",
        "pairwise_preferences",
        "feedback_signals",
        "image_generation_runs",
        "generated_asset_lineage",
        "intelligence_experiments",
        "representation_sets",
        "representation_set_items",
        "intelligence_agent_runs",
        "intelligence_agent_steps",
        "preference_datasets",
        "preference_dataset_items",
        "preference_model_versions",
        "annotation_refresh_runs",
        "annotation_refresh_items",
        "blind_studies",
        "blind_study_cases",
        "blind_study_responses",
        "active_learning_batches",
        "active_learning_selections",
        "shadow_editorial_decisions",
        "candidate_exposures",
        "multimodal_rerank_runs",
        "composed_retrieval_examples",
    }
    assert required_tables <= set(upgraded.get_table_names())
    assert "caption_slate_id" in {column["name"] for column in upgraded.get_columns("proposals")}
    with upgraded_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM posts")).scalar_one() == post_count
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0011_runtime_performance"
        )
    upgraded_engine.dispose()
