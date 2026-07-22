from fastapi.testclient import TestClient
from sqlalchemy import text

from runway.api.app import create_app
from runway.config import Settings
from runway.db import Database
from runway.db.models import PairwisePreference
from runway.db.repositories import get_channel


def test_migrations_seed_channel_and_enable_wal(database: Database) -> None:
    with database.session() as session:
        channel_count = session.execute(text("SELECT count(*) FROM channels")).scalar_one()
        journal_mode = session.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = session.execute(text("PRAGMA foreign_keys")).scalar_one()
        migration = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        feedback_table = session.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='caption_feedback'"
            )
        ).scalar_one()
        publisher_table = session.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='publish_attempts'"
            )
        ).scalar_one()
        schedule_slot = session.execute(
            text(
                "SELECT count(*) FROM pragma_table_info('proposals') "
                "WHERE name='scheduled_publish_at'"
            )
        ).scalar_one()
        planning_horizon = session.execute(
            text("SELECT planning_horizon_days FROM channels LIMIT 1")
        ).scalar_one()
        canonical_tables = session.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('representation_records', 'intelligence_retrieval_runs', "
                "'caption_slates', 'caption_candidate_records', "
                "'image_generation_runs', 'intelligence_experiments')"
            )
        ).scalar_one()
        caption_slate_column = session.execute(
            text(
                "SELECT count(*) FROM pragma_table_info('proposals') WHERE name='caption_slate_id'"
            )
        ).scalar_one()
    assert channel_count == 1
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert migration == "0010_neural_intelligence"
    assert feedback_table == 1
    assert publisher_table == 1
    assert schedule_slot == 1
    assert planning_horizon == 0
    assert canonical_tables == 6
    assert caption_slate_column == 1


def test_health_app_is_local_and_publishing_is_disabled(settings: Settings) -> None:
    app = create_app(settings)
    health_route = next(route for route in app.routes if getattr(route, "path", None) == "/health")
    payload = health_route.endpoint()
    assert payload["status"] == "ok"
    assert payload["publishing_enabled"] is False


def test_lineup_mutations_require_literal_confirmation(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        update = client.patch(
            "/api/lineup/1",
            json={"final_caption": "Why now?!", "confirmed": False},
        )
        removal = client.post(
            "/api/lineup/1/remove",
            json={"confirmed": False},
        )
        push = client.post(
            "/api/lineup/push",
            json={"confirmed": False},
        )

    assert update.status_code == 422
    assert removal.status_code == 422
    assert push.status_code == 422


def test_passive_connection_and_intelligence_status_are_truthful(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        connection = client.get("/api/publisher/session/status")
        intelligence = client.get("/api/intelligence/status")

    assert connection.status_code == 200
    assert connection.json()["state"] == "disabled"
    assert connection.json()["checked_at"] is None
    assert intelligence.status_code == 200
    assert intelligence.json()["feedback_signals"] == {
        "caption": 0,
        "image": 0,
        "pairing": 0,
        "total": 0,
    }
    assert intelligence.json()["training_pairwise_labels_by_target"] == {
        "caption": 0,
        "image": 0,
        "pairing": 0,
    }
    assert intelligence.json()["active_model_targets"] == []


def test_intelligence_readiness_excludes_nonhuman_engineering_fixtures(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        for target, count, split in (("caption", 8, "development"),):
            for index in range(count):
                session.add(
                    PairwisePreference(
                        channel_id=channel_id,
                        preferred_text=f"Preferred {target} {index}",
                        dispreferred_text=f"Other {target} {index}",
                        preference_source="offline_engineering_fixture",
                        label_source="engineering_fixture",
                        target=target,
                        learning_split=split,
                        idempotency_key=f"readiness:{target}:{index}",
                    )
                )

    with TestClient(create_app(settings)) as client:
        collecting = client.get("/api/intelligence/status").json()

    assert collecting["human_pairwise_labels"] == 0
    assert collecting["training_pairwise_labels_by_target"] == {
        "caption": 0,
        "image": 0,
        "pairing": 0,
    }
    assert collecting["trainable_targets"] == []
    assert collecting["state"] == "collecting_creator_labels"
    assert collecting["engineering_minimum_labels_by_target"]["caption"] == 8
    assert collecting["product_challenger_minimum_labels_by_target"]["caption"] == 100
