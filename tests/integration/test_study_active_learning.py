from __future__ import annotations

import json
import os
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


def test_blind_export_removes_compound_and_nested_provenance_metadata(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    case = _study_cases(database, count=1)[0].model_copy(
        update={
            "metadata": {
                "scene": "unicode-ü-fixture",
                "baseline_model": "baseline",
                "nested": {
                    "challenger_side": "challenger",
                    "safe_detail": "kept",
                },
                "score_breakdown": {"value": 0.9},
                "safe_list": [{"origin": "baseline", "label": "kept"}, "challenger", "kept"],
                "empty_object": {},
                "empty_list": [],
                "zero": 0,
                "negative": -1,
                "false": False,
                "null": None,
            }
        }
    )
    planned = service.plan(
        [case],
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )

    output = tmp_path / "blind.json"
    service.export(int(planned["study_id"]), output, minimum_cases=1)
    exported = json.loads(output.read_text(encoding="utf-8"))

    assert exported["cases"][0]["metadata"] == {
        "scene": "unicode-ü-fixture",
        "nested": {"safe_detail": "kept"},
        "safe_list": [{"label": "kept"}, "kept"],
        "empty_object": {},
        "empty_list": [],
        "zero": 0,
        "negative": -1,
        "false": False,
        "null": None,
    }


def test_blind_export_rejects_json_escaped_provenance_identity(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    secret = "secret\nmodel-ü"
    case = _study_cases(database, count=1)[0].model_copy(
        update={"metadata": {"notes": f"prefix {secret} suffix"}}
    )
    planned = service.plan(
        [case],
        baseline_identity=secret,
        challenger_identity="other-model",
        seed=42,
    )

    output = tmp_path / "blind.json"
    with pytest.raises(RuntimeError, match="would reveal hidden model provenance"):
        service.export(int(planned["study_id"]), output, minimum_cases=1)
    assert not output.exists()


def test_blind_export_rejects_nested_and_keyed_escaped_provenance_identity(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    base_case = _study_cases(database, count=1)[0]
    metadata_variants = [
        ("secret\nmodel-ü", {"nested": {"safe_list": ["prefix secret\nmodel-ü suffix"]}}),
        ("secret\nidentity-ü", {"safe-key-secret\nidentity-ü": "kept"}),
    ]
    for index, (secret, metadata) in enumerate(metadata_variants):
        case = base_case.model_copy(update={"metadata": metadata})
        planned = service.plan(
            [case],
            baseline_identity=secret,
            challenger_identity="other-model",
            seed=42,
        )

        output = tmp_path / f"blind-{index}.json"
        with pytest.raises(RuntimeError, match="would reveal hidden model provenance"):
            service.export(int(planned["study_id"]), output, minimum_cases=1)
        assert not output.exists()


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), float("-inf")])
def test_blind_export_rejects_nonfinite_metadata_numbers(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    bad_number: float,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    case = _study_cases(database, count=1)[0].model_copy(
        update={"metadata": {"bad_number": bad_number}}
    )
    planned = service.plan(
        [case],
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )

    output = tmp_path / "blind.json"
    with pytest.raises(ValueError, match="not JSON compliant"):
        service.export(int(planned["study_id"]), output, minimum_cases=1)
    assert not output.exists()


def test_blind_export_uses_temporary_sibling_then_atomic_rename(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    planned = service.plan(
        _study_cases(database, count=1),
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )
    output = tmp_path / "blind.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: object, destination: object) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    service.export(int(planned["study_id"]), output, minimum_cases=1)

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert destination == output
    assert temporary.parent == output.parent
    assert temporary != output
    assert not temporary.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["case_count"] == 1


@pytest.mark.parametrize("has_existing_export", [False, True])
def test_blind_export_interruption_cannot_leave_partial_final_export(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_existing_export: bool,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    planned = service.plan(
        _study_cases(database, count=1),
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )
    output = tmp_path / "blind.json"
    if has_existing_export:
        output.write_text("complete prior export\n", encoding="utf-8")

    def interrupted_replace(source: object, destination: object) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        service.export(int(planned["study_id"]), output, minimum_cases=1)

    if has_existing_export:
        assert output.read_text(encoding="utf-8") == "complete prior export\n"
    else:
        assert not output.exists()
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


@pytest.mark.parametrize("has_existing_export", [False, True])
def test_blind_export_write_interruption_cleans_temporary_file(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_existing_export: bool,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    planned = service.plan(
        _study_cases(database, count=1),
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )
    output = tmp_path / "blind.json"
    if has_existing_export:
        output.write_text("complete prior export\n", encoding="utf-8")

    class InterruptedWriter:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor
            self.closed = False

        def __enter__(self) -> InterruptedWriter:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

        def write(self, content: str) -> int:
            raise OSError("simulated write interruption")

        def close(self) -> None:
            if not self.closed:
                os.close(self.descriptor)
                self.closed = True

    writers: list[InterruptedWriter] = []

    def interrupted_fdopen(descriptor: int, *args: object, **kwargs: object) -> InterruptedWriter:
        writer = InterruptedWriter(descriptor)
        writers.append(writer)
        return writer

    monkeypatch.setattr(os, "fdopen", interrupted_fdopen)
    with pytest.raises(OSError, match="simulated write interruption"):
        service.export(int(planned["study_id"]), output, minimum_cases=1)

    if has_existing_export:
        assert output.read_text(encoding="utf-8") == "complete prior export\n"
    else:
        assert not output.exists()
    assert writers[0].closed
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


def test_blind_export_long_output_filename_does_not_inflate_temporary_name(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    planned = service.plan(
        _study_cases(database, count=1),
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )
    output = tmp_path / ("a" * 250 + ".json")
    assert len(output.name.encode("utf-8")) == 255

    service.export(int(planned["study_id"]), output, minimum_cases=1)

    assert json.loads(output.read_text(encoding="utf-8"))["case_count"] == 1
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


def test_blind_export_overwrite_remains_deterministic(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = BlindStudyService(database, settings)
    planned = service.plan(
        _study_cases(database, count=1),
        baseline_identity="secret-model-a",
        challenger_identity="secret-model-b",
        seed=42,
    )
    output = tmp_path / "blind.json"
    output.write_text("stale partial export\n", encoding="utf-8")

    service.export(int(planned["study_id"]), output, minimum_cases=1)
    first_export = output.read_bytes()
    assert first_export != b"stale partial export\n"

    service.export(int(planned["study_id"]), output, minimum_cases=1)
    assert output.read_bytes() == first_export
    assert json.loads(first_export.decode("utf-8"))["case_count"] == 1


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


def _active_learning_service_and_batch(
    database: Database,
    settings: Settings,
) -> tuple[ActiveLearningService, int]:
    service = ActiveLearningService(database, settings)
    selected = service.select(
        [
            ActiveLearningCase(
                entity_type="fixture",
                entity_id=1,
                group_key="group-atomic-export",
                split="development",
                target="caption",
                uncertainty_score=0.9,
                disagreement_score=0.4,
                information_gain_score=0.5,
                diversity_cluster="atomic-export-cluster",
                reasons=["fixture"],
                metadata={"scene": "unicode-ü", "nested": {"zero": 0, "empty": []}},
            )
        ],
        target="caption",
        limit=1,
        seed=7,
    )
    return service, int(selected["batch_id"])


def test_active_learning_export_uses_temporary_sibling_then_atomic_rename(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, batch_id = _active_learning_service_and_batch(database, settings)
    output = tmp_path / "active-learning.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: object, destination: object) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    service.export(batch_id, output)

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert destination == output
    assert temporary.parent == output.parent
    assert temporary != output
    assert temporary.name.endswith(".tmp")
    assert not temporary.exists()
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["batch_id"] == batch_id
    assert exported["selections"][0]["rationale"]["metadata"]["scene"] == "unicode-ü"


@pytest.mark.parametrize("has_existing_export", [False, True])
def test_active_learning_export_interruption_cannot_leave_partial_final_export(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_existing_export: bool,
) -> None:
    service, batch_id = _active_learning_service_and_batch(database, settings)
    output = tmp_path / "active-learning.json"
    if has_existing_export:
        output.write_text("complete prior export\n", encoding="utf-8")

    def interrupted_replace(source: object, destination: object) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        service.export(batch_id, output)

    if has_existing_export:
        assert output.read_text(encoding="utf-8") == "complete prior export\n"
    else:
        assert not output.exists()
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


def test_active_learning_export_overwrite_preserves_nested_unicode_content(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    service, batch_id = _active_learning_service_and_batch(database, settings)
    output = tmp_path / "nested" / "active-learning.json"
    output.parent.mkdir()
    output.write_text("stale partial export\n", encoding="utf-8")

    service.export(batch_id, output)

    raw = output.read_text(encoding="utf-8")
    assert raw != "stale partial export\n"
    exported = json.loads(raw)
    assert exported["batch_id"] == batch_id
    assert exported["selections"][0]["rationale"]["metadata"] == {
        "scene": "unicode-ü",
        "nested": {"zero": 0, "empty": []},
    }
    assert "unicode-ü" in raw
