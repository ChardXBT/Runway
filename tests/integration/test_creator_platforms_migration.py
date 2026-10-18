from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from runway.config import Settings

NEW_TABLES = {
    "connected_accounts",
    "oauth_states",
    "content_groups",
    "platform_content",
    "content_metric_snapshots",
    "account_metric_snapshots",
    "account_metric_days",
    "comment_records",
    "reply_drafts",
    "platform_actions",
    "sync_runs",
    "sync_cursors",
    "content_drafts",
    "draft_targets",
}


def _alembic_config(settings: Settings) -> Config:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(settings.project_root / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def _seed_0011_rows(engine) -> None:
    now = "2026-10-01 00:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO channels (name, handle, youtube_channel_id, timezone, "
                "default_post_time, planning_horizon_days, duplicate_window_days, "
                "created_at, updated_at) VALUES "
                "('Northwind', '@northwindstudio', NULL, 'UTC', '09:00', 7, 14, "
                f"'{now}', '{now}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO proposals (generation_run_id, channel_id, candidate_image_id, "
                "caption_slate_id, backup_candidate_ids_json, planned_publish_at, "
                "scheduled_publish_at, recommended_caption, alternative_captions_json, "
                "caption_rationale, caption_confidence, caption_reference_post_ids_json, "
                "factual_uncertainty_warning, final_caption, status, selection_reason, "
                "style_score, novelty_score, quality_score, "
                "closest_historical_matches_json, warnings_json, rights_decision, "
                "rights_reviewed_at, approved_at, rejected_at, external_post_id, "
                "external_post_url, scheduled_verified_at, created_at, updated_at) VALUES "
                "(0, 1, 0, NULL, '[]', '2026-10-02T09:00:00', NULL, 'cap', '[]', '', "
                "NULL, '[]', NULL, 'cap', 'draft', 'seed', 0.5, 0.5, 0.5, '[]', '[]', "
                f"NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{now}', '{now}')"
            )
        )


def test_0012_upgrade_and_downgrade_preserve_existing_rows(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "migration-data")
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    config = _alembic_config(settings)
    command.upgrade(config, "0011_runtime_performance")

    engine = create_engine(settings.database_url)
    _seed_0011_rows(engine)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(settings.database_url)
    upgraded = inspect(engine)
    assert set(upgraded.get_table_names()) >= NEW_TABLES
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM channels")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM proposals")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0012_creator_platforms"
        )
    engine.dispose()

    command.downgrade(config, "0011_runtime_performance")
    engine = create_engine(settings.database_url)
    downgraded = inspect(engine)
    assert NEW_TABLES.isdisjoint(downgraded.get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM channels")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM proposals")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0011_runtime_performance"
        )
    engine.dispose()
