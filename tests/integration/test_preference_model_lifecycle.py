from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import func, select

from runway.captions.feature_snapshots import (
    FEATURE_SCHEMA_VERSION,
    snapshot_json_and_hash,
)
from runway.captions.preference_models import (
    REQUIRED_PREFERENCE_ACTIVATION_GATES,
    InsufficientPreferenceData,
    PreferenceDatasetService,
    PreferenceModelService,
)
from runway.captions.preferences import PairwiseCaptionPreferenceRanker
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    PairwisePreference,
    PreferenceDatasetItem,
    PreferenceModelVersion,
)
from runway.db.repositories import get_channel


def _passing_gates() -> dict[str, bool]:
    return {gate: True for gate in REQUIRED_PREFERENCE_ACTIVATION_GATES}


def _seed_pairs(
    database: Database,
    settings: Settings,
    *,
    target: str,
    count: int = 24,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        for index in range(count):
            preferred_text = f"Why is everyone so excited? {index}"
            dispreferred_text = f"Everyone is very excited. {index}"
            preferred_json, preferred_hash = snapshot_json_and_hash(
                preferred_text,
                {
                    "grounding": 0.95,
                    "policy": 0.95,
                    "style": 0.85,
                    "novelty": 0.8,
                    "rotation": 0.8,
                    "positive_feedback": 0.8,
                    "negative_feedback_risk": 0.0,
                    "pairing": 0.9,
                },
            )
            dispreferred_json, dispreferred_hash = snapshot_json_and_hash(
                dispreferred_text,
                {
                    "grounding": 0.7,
                    "policy": 0.5,
                    "style": 0.4,
                    "novelty": 0.3,
                    "rotation": 0.3,
                    "positive_feedback": 0.0,
                    "negative_feedback_risk": 0.6,
                    "pairing": 0.5,
                },
            )
            combined_hash = hashlib.sha256(
                f"{target}:{index}:{preferred_hash}:{dispreferred_hash}".encode()
            ).hexdigest()
            session.add(
                PairwisePreference(
                    channel_id=channel_id,
                    preferred_text=preferred_text,
                    dispreferred_text=dispreferred_text,
                    preference_source="offline_engineering_fixture",
                    label_source="engineering_fixture",
                    strength=1.0,
                    reason_codes_json="[]",
                    target=target,
                    source_event_key=f"study:{target}:{index}",
                    derivation_version="blind-study-import-v1",
                    idempotency_key=f"study-pair:{target}:{index}",
                    preferred_features_json=preferred_json,
                    dispreferred_features_json=dispreferred_json,
                    context_snapshot_json=json.dumps(
                        {"study_case": index},
                        sort_keys=True,
                    ),
                    feature_schema_version=FEATURE_SCHEMA_VERSION,
                    feature_snapshot_hash=combined_hash,
                    group_key=f"{target}-group-{index}",
                    taxonomy_version="caption-taxonomy-v1",
                    verifier_version="caption-verifier-v1",
                    representation_sets_json="{}",
                )
            )


def test_preference_dataset_and_model_are_reproducible_and_score_without_refit(
    database: Database,
    settings: Settings,
) -> None:
    _seed_pairs(database, settings, target="caption")
    datasets = PreferenceDatasetService(database, settings)
    first_dataset = datasets.build(
        "caption",
        label_sources=frozenset({"engineering_fixture"}),
        engineering_test=True,
    )
    second_dataset = datasets.build(
        "caption",
        label_sources=frozenset({"engineering_fixture"}),
        engineering_test=True,
    )
    assert first_dataset["dataset_id"] == second_dataset["dataset_id"]
    assert second_dataset["created"] is False

    with database.session() as session:
        items = session.scalars(
            select(PreferenceDatasetItem).where(
                PreferenceDatasetItem.dataset_id == first_dataset["dataset_id"]
            )
        ).all()
        by_group: dict[str, set[str]] = {}
        for item in items:
            by_group.setdefault(item.group_key, set()).add(item.split)
    assert all(len(splits) == 1 for splits in by_group.values())

    models = PreferenceModelService(database, settings)
    first_model = models.train(
        "caption",
        dataset_id=str(first_dataset["dataset_id"]),
    )
    repeated_model = models.train(
        "caption",
        dataset_id=str(first_dataset["dataset_id"]),
    )
    assert first_model["model_version_id"] == repeated_model["model_version_id"]
    assert repeated_model["created"] is False
    assert first_model["calibration"]["calibrated"] is False  # type: ignore[index]
    with pytest.raises(ValueError, match="activation gates"):
        models.activate(
            int(first_model["model_version_id"]),
            reason="must fail",
            gate_results={"quality": False},
            activation_tier="engineering_test",
        )
    with pytest.raises(ValueError, match="100 genuine human"):
        models.activate(
            int(first_model["model_version_id"]),
            reason="product threshold must stay closed",
            gate_results=_passing_gates(),
        )
    models.activate(
        int(first_model["model_version_id"]),
        reason="fixture gates passed",
        gate_results=_passing_gates(),
        activation_tier="engineering_test",
    )

    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        before = int(session.scalar(select(func.count(PreferenceModelVersion.id))) or 0)
    ranker = PairwiseCaptionPreferenceRanker(database)
    first_score = ranker.score(
        channel_id=channel_id,
        text="Why is Homer so excited?",
        components={
            "grounding": 0.9,
            "policy": 0.9,
            "style": 0.8,
            "novelty": 0.7,
            "rotation": 0.7,
            "positive_feedback": 0.5,
            "negative_feedback_risk": 0.0,
            "pairing": 0.8,
        },
    )
    second_score = ranker.score(
        channel_id=channel_id,
        text="Why is Homer so excited?",
        components={
            "grounding": 0.9,
            "policy": 0.9,
            "style": 0.8,
            "novelty": 0.7,
            "rotation": 0.7,
            "positive_feedback": 0.5,
            "negative_feedback_risk": 0.0,
            "pairing": 0.8,
        },
    )
    with database.session() as session:
        after = int(session.scalar(select(func.count(PreferenceModelVersion.id))) or 0)
    assert first_score == second_score
    assert first_score.trained is True
    assert first_score.model_version_id == first_model["model_version_id"]
    assert before == after

    challenger = models.train(
        "caption",
        dataset_id=str(first_dataset["dataset_id"]),
        epochs=201,
    )
    models.activate(
        int(challenger["model_version_id"]),
        reason="fixture challenger",
        gate_results=_passing_gates(),
        activation_tier="engineering_test",
    )
    restored = models.rollback(
        int(challenger["model_version_id"]),
        reason="fixture regression",
    )
    assert restored["model_version_id"] == first_model["model_version_id"]
    assert restored["active"] is True


def test_preference_targets_are_separate_and_thresholds_fail_closed(
    database: Database,
    settings: Settings,
) -> None:
    models = PreferenceModelService(database, settings)
    with pytest.raises(InsufficientPreferenceData, match="requires 8 labels"):
        models.train("image")
    with database.session() as session:
        insufficient = session.scalar(
            select(PreferenceModelVersion).where(
                PreferenceModelVersion.target == "image",
                PreferenceModelVersion.status == "insufficient_data",
            )
        )
        assert insufficient is not None
        assert insufficient.active is False

    _seed_pairs(database, settings, target="image")
    _seed_pairs(database, settings, target="pairing")
    datasets = PreferenceDatasetService(database, settings)
    image_dataset = datasets.build(
        "image",
        label_sources=frozenset({"engineering_fixture"}),
        engineering_test=True,
    )
    pairing_dataset = datasets.build(
        "pairing",
        label_sources=frozenset({"engineering_fixture"}),
        engineering_test=True,
    )
    image_model = models.train("image", dataset_id=str(image_dataset["dataset_id"]))
    pairing_model = models.train("pairing", dataset_id=str(pairing_dataset["dataset_id"]))
    models.activate(
        int(image_model["model_version_id"]),
        reason="image fixture",
        gate_results=_passing_gates(),
        activation_tier="engineering_test",
    )
    models.activate(
        int(pairing_model["model_version_id"]),
        reason="pairing fixture",
        gate_results=_passing_gates(),
        activation_tier="engineering_test",
    )
    with database.session() as session:
        active_targets = set(
            session.scalars(
                select(PreferenceModelVersion.target).where(PreferenceModelVersion.active.is_(True))
            )
        )
    assert active_targets == {"image", "pairing"}


def test_preference_activation_rejects_incompatible_or_tampered_artifacts(
    database: Database,
    settings: Settings,
) -> None:
    _seed_pairs(database, settings, target="caption")
    service = PreferenceModelService(database, settings)
    dataset = PreferenceDatasetService(database, settings).build(
        "caption",
        label_sources=frozenset({"engineering_fixture"}),
        engineering_test=True,
    )
    incompatible = service.train("caption", dataset_id=str(dataset["dataset_id"]))
    incompatible_id = int(incompatible["model_version_id"])
    with database.session() as session:
        row = session.get(PreferenceModelVersion, incompatible_id)
        assert row is not None
        row.feature_schema_version = "obsolete-feature-schema"
    with pytest.raises(ValueError, match="feature schema"):
        service.activate(
            incompatible_id,
            reason="must reject incompatible schema",
            gate_results=_passing_gates(),
            activation_tier="engineering_test",
        )

    with database.session() as session:
        row = session.get(PreferenceModelVersion, incompatible_id)
        assert row is not None
        row.feature_schema_version = FEATURE_SCHEMA_VERSION
        parameters = json.loads(row.parameters_json)
        assert isinstance(parameters, dict)
        weights = parameters["weights"]
        assert isinstance(weights, list)
        weights[0] = float("nan")
        row.parameters_json = json.dumps(parameters)
    with pytest.raises(ValueError, match="parameters failed validation"):
        service.activate(
            incompatible_id,
            reason="must reject malformed parameters",
            gate_results=_passing_gates(),
            activation_tier="engineering_test",
        )
    with database.session() as session:
        row = session.get(PreferenceModelVersion, incompatible_id)
        assert row is not None
        assert row.active is False
