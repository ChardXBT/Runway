from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    Channel,
    IntelligenceExperiment,
    PairwisePreference,
    RepresentationSet,
    RepresentationSetItem,
)
from runway.intelligence.doctor import IntelligenceDoctor
from runway.intelligence.representation_sets import RepresentationSetService


def _finish(service: RepresentationSetService, set_id: int) -> None:
    for _ in range(20):
        result = service.backfill(set_id, batch_size=10)
        if result["complete"] == result["expected"]:
            return
    raise AssertionError("representation fixture did not complete")


def test_doctor_detects_conflicting_active_sets_and_stale_vectors(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = RepresentationSetService(database, settings)
    planned = service.plan_history("text")
    set_id = int(planned["representation_set_id"])
    _finish(service, set_id)
    assert service.validate(set_id)["valid"] is True
    service.activate(set_id, reason="doctor fixture")
    with database.session() as session:
        original = session.get(RepresentationSet, set_id)
        assert original is not None
        conflicting = RepresentationSet(
            channel_id=original.channel_id,
            scope=original.scope,
            purpose=original.purpose,
            modality=original.modality,
            provider=original.provider,
            model=original.model,
            model_version=original.model_version,
            configuration_json=original.configuration_json,
            configuration_hash=original.configuration_hash,
            plan_hash="f" * 64,
            status="active",
            expected_count=0,
            completed_count=0,
            failed_count=0,
            stale_count=0,
            active=True,
        )
        session.add(conflicting)
        item = session.scalar(
            select(RepresentationSetItem).where(
                RepresentationSetItem.representation_set_id == set_id
            )
        )
        assert item is not None
        item.source_content_hash = "0" * 64

    report = IntelligenceDoctor(
        database,
        settings,
        verify_media_files=False,
    ).run()
    codes = {finding.code for finding in report.findings}
    assert report.critical_count >= 2
    assert "representations.conflicting_active_sets" in codes
    assert "representations.stale_or_mismatched_items" in codes
    assert report.as_dict()["status"] == "failed"


def test_doctor_detects_duplicate_derived_labels(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    snapshot = json.dumps(
        {
            "feature_schema_version": "caption-preference-features-v1",
            "features": {"bias": 1.0},
        },
        sort_keys=True,
    )
    with database.session() as session:
        for index in range(2):
            session.add(
                PairwisePreference(
                    channel_id=1,
                    preferred_text="A",
                    dispreferred_text="B",
                    preference_source="fixture",
                    label_source="human",
                    target="caption",
                    source_event_key="duplicate-event",
                    derivation_version="fixture-v1",
                    idempotency_key=None,
                    preferred_features_json=snapshot,
                    dispreferred_features_json=snapshot,
                    context_snapshot_json="{}",
                    feature_schema_version="caption-preference-features-v1",
                    feature_snapshot_hash=f"{index + 1:064x}",
                    group_key="fixture",
                    representation_sets_json="{}",
                    learning_split="development",
                )
            )
    report = IntelligenceDoctor(
        database,
        settings,
        verify_media_files=False,
    ).run()
    assert any(finding.code == "learning.duplicate_derivation" for finding in report.findings)
    assert report.critical_count > 0


def test_doctor_detects_channel_leakage_and_missing_artifacts(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = RepresentationSetService(database, settings)
    planned = service.plan_history("text")
    set_id = int(planned["representation_set_id"])
    _finish(service, set_id)
    with database.session() as session:
        other = Channel(
            name="Other",
            handle="doctor-other",
            timezone="UTC",
            default_post_time="10:00",
            planning_horizon_days=0,
            duplicate_window_days=30,
        )
        session.add(other)
        session.flush()
        item = session.scalar(
            select(RepresentationSetItem)
            .where(RepresentationSetItem.representation_set_id == set_id)
            .order_by(RepresentationSetItem.id)
            .limit(1)
        )
        assert item is not None
        item.channel_id = other.id
        session.add(
            IntelligenceExperiment(
                experiment_id="doctor-missing-artifact",
                channel_id=1,
                hypothesis="The doctor should report absent experiment assets.",
                baseline_commit="0" * 40,
                datasets_json="[]",
                configuration_json="{}",
                configuration_hash="a" * 64,
                completed_at=datetime.now(UTC),
                metrics_json=json.dumps({"fixture": True}),
                artifacts_json=json.dumps([str(tmp_path / "missing-model-artifact.json")]),
            )
        )

    report = IntelligenceDoctor(
        database,
        settings,
        verify_media_files=False,
    ).run()
    codes = {finding.code for finding in report.findings}
    assert "channel_isolation.representation_set_item" in codes
    assert "experiments.missing_artifacts" in codes
    assert report.critical_count > 0
