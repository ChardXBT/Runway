from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import BlindStudyResponse, PairwisePreference, PostMedia
from runway.intelligence.studies import (
    ActiveLearningCase,
    ActiveLearningService,
    BlindStudyCasePlan,
    BlindStudyImportResponse,
    BlindStudyService,
)


def _study_cases(
    database: Database,
    *,
    count: int = 50,
) -> list[BlindStudyCasePlan]:
    with database.session() as session:
        source_ids = list(
            session.scalars(select(PostMedia.media_asset_id).order_by(PostMedia.media_asset_id))
        )
    # The fixture has fewer than 50 distinct images. Tests use unique case-owned
    # records by cycling only after cloning is unnecessary for export mechanics.
    # Planning correctly requires unique media, so the caller controls count.
    return [
        BlindStudyCasePlan(
            case_key=f"case-{index:03d}",
            media_asset_id=media_id,
            group_key=f"group-{index:03d}",
            split=(
                "final_holdout"
                if index % 5 == 0
                else ("tuning" if index % 4 == 0 else "development")
            ),
            baseline_caption=f"Baseline question {index}?",
            challenger_caption=f"What happens next in scene {index}?",
            metadata={"scene": index},
            selection_rationale={"uncertainty": 0.5},
        )
        for index, media_id in enumerate(source_ids[:count], start=1)
    ]


def test_blind_order_export_and_offline_fixture_import_are_provenance_safe(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    cases = _study_cases(database, count=9)
    first = service.plan(
        cases,
        baseline_identity="baseline-secret",
        challenger_identity="challenger-secret",
        seed=42,
    )
    repeated = service.plan(
        cases,
        baseline_identity="baseline-secret",
        challenger_identity="challenger-secret",
        seed=42,
    )
    assert first["study_id"] == repeated["study_id"]
    assert repeated["created"] is False

    output = tmp_path / "blind.json"
    service.export(int(first["study_id"]), output, minimum_cases=9)
    exported = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(exported)
    assert "baseline-secret" not in serialized
    assert "challenger-secret" not in serialized
    assert "first_origin" not in serialized
    assert [row["case_key"] for row in exported["cases"]] != sorted(
        row["case_key"] for row in exported["cases"]
    )

    responses = [
        BlindStudyImportResponse(
            case_key=row["case_key"],
            choice="first" if index % 2 else "second",
            acceptable_choices=["first"],
            caption_verdict="accepted",
            pairing_verdict="accepted",
            decision_time_ms=1200 + index,
        )
        for index, row in enumerate(exported["cases"], start=1)
    ]
    imported = service.import_responses(
        int(first["study_id"]),
        responses,
        review_session="creator-session-1",
        reviewer_label="offline-test-fixture",
        reviewer_kind="engineering_fixture",
    )
    assert imported["response_count"] == 9
    assert imported["pairwise_preferences_created"] == 9
    report = service.report(int(first["study_id"]))
    assert imported["human_response_count"] == 0
    assert report["sample_size"] == 0
    assert report["synthetic_labels"] == 0
    with database.session() as session:
        assert session.scalar(select(func.count(BlindStudyResponse.id))) == 9
        pairs = session.scalars(select(PairwisePreference)).all()
    assert len(pairs) == 9
    assert {pair.label_source for pair in pairs} == {"engineering_fixture"}
    assert all(pair.source_study_response_id is not None for pair in pairs)
    assert any(pair.learning_split == "final_holdout" for pair in pairs)
    with pytest.raises(ValueError, match="duplicate labels"):
        service.import_responses(
            int(first["study_id"]),
            responses[:1],
            review_session="creator-session-2",
            reviewer_label="offline-test-fixture",
            reviewer_kind="engineering_fixture",
        )


def test_active_learning_prioritizes_uncertainty_and_preserves_diversity(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    cases = [
        ActiveLearningCase(
            entity_type="fixture",
            entity_id=index,
            group_key=f"group-{index}",
            split="development",
            target="caption",
            uncertainty_score=0.99 if index in {1, 2} else 0.5,
            disagreement_score=0.8 if index == 3 else 0.2,
            information_gain_score=0.5,
            diversity_cluster="crowded" if index in {1, 2} else f"cluster-{index}",
            reasons=["fixture"],
        )
        for index in range(1, 7)
    ]
    service = ActiveLearningService(database, settings)
    first = service.select(cases, target="caption", limit=3, seed=7)
    repeated = service.select(cases, target="caption", limit=3, seed=7)
    assert first["batch_id"] == repeated["batch_id"]
    assert repeated["created"] is False
    selections = first["selections"]
    assert selections[0]["entity_id"] in {1, 2}
    clusters = [row["rationale"]["diversity_cluster"] for row in selections]
    assert len(set(clusters)) == 3
