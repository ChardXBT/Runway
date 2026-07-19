from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from runway.evaluation.datasets import DatasetRepository
from runway.evaluation.experiments import ExperimentRunner
from runway.evaluation.gates import (
    CRITICAL_REGRESSIONS,
    HARD_GATES,
    replacement_gate_report,
)
from runway.evaluation.scoring import DEFAULT_WEIGHTS
from runway.evaluation.statistics import preference_summary, wilson_interval


def _candidate_artifact(
    repository: DatasetRepository,
    split: str,
) -> dict[str, Any]:
    labels = repository.labels(
        split,  # type: ignore[arg-type]
        purpose="holdout_release" if split == "locked_holdout" else "tuning",
    )
    return {
        "dataset_version": repository.canonical().dataset_version,
        "splits": [split],
        "cases": [
            {
                "case_id": label.case_id,
                "generated_pool": [
                    {
                        "text": (
                            f"{label.expected_terms[0]} "
                            f"{label.expected_terms[-1]}?"
                        ),
                        "structure": label.preferred_structures[0],
                        "eligible": True,
                        "verification": {
                            "passed": True,
                            "grounding_score": 1.0,
                            "unsupported_claims": [],
                        },
                        "components": {
                            "preference": 0.7,
                            "grounding": 1.0,
                            "policy": 1.0,
                            "style": 0.7,
                            "novelty": 0.8,
                            "rotation": 0.8,
                            "positive_feedback": 0.0,
                            "negative_feedback_risk": 0.0,
                            "pairing": 0.8,
                            "structure_fit": 1.0,
                            "length_fit": 1.0,
                        },
                        "generic_penalty": 0.0,
                    }
                ],
            }
            for label in labels
        ],
    }


def test_dataset_boundaries_cluster_integrity_and_blind_order_are_deterministic() -> None:
    repository = DatasetRepository()
    assert repository.validate_split_integrity()["passed"] is True
    with pytest.raises(PermissionError, match="locked holdout"):
        repository.labels("locked_holdout", purpose="tuning")
    with pytest.raises(PermissionError, match="only tuning"):
        repository.labels("development", purpose="tuning")

    first = repository.randomized_blind_preferences(seed=20260718)
    second = repository.randomized_blind_preferences(seed=20260718)
    assert first == second
    assert {row["order_token"] for row in first} <= {"original", "swapped"}


def test_frozen_baseline_checksums_are_immutable() -> None:
    root = (
        DatasetRepository().root.parent
        / "baseline-876fe5f"
    )
    for line in (root / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        expected, filename = line.split(maxsplit=1)
        actual = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        assert actual == expected
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["baseline_commit"] == "876fe5f814b1a58f0d11b9eaf29f4c508f20595d"
    assert manifest["paid_provider_calls"] == 0
    assert manifest["publishing_enabled"] is False


def test_tuning_is_reproducible_and_holdout_can_run_only_once(
    tmp_path: Path,
) -> None:
    repository = DatasetRepository()
    runner = ExperimentRunner(
        datasets=repository,
        artifact_root=tmp_path / "experiments",
        seed=20260718,
    )
    tuning = _candidate_artifact(repository, "tuning")
    first = runner.tune(tuning)
    second = runner.tune(tuning)
    assert first["winner"]["configuration_hash"] == second["winner"]["configuration_hash"]
    assert first["configuration_count"] == 6
    assert all(
        experiment["metrics"] == repeated["metrics"]
        for experiment, repeated in zip(
            first["experiments"],
            second["experiments"],
            strict=True,
        )
    )
    changed = deepcopy(tuning)
    changed["cases"][0]["generated_pool"][0]["components"]["style"] = 0.1
    changed_run = runner.tune(changed)
    original_ids = {
        experiment["experiment_id"] for experiment in first["experiments"]
    }
    changed_ids = {
        experiment["experiment_id"] for experiment in changed_run["experiments"]
    }
    assert original_ids.isdisjoint(changed_ids)
    assert all(
        (tmp_path / "experiments" / experiment_id / "experiment.json").is_file()
        for experiment_id in original_ids | changed_ids
    )

    holdout = _candidate_artifact(repository, "locked_holdout")
    result = runner.evaluate_holdout(
        holdout,
        winning_weights=DEFAULT_WEIGHTS,
    )
    assert result["use_count"] == 1
    with pytest.raises(RuntimeError, match="already been evaluated"):
        runner.evaluate_holdout(
            holdout,
            winning_weights=DEFAULT_WEIGHTS,
        )


def test_statistics_and_replacement_gate_are_reproducible_and_fail_closed() -> None:
    assert wilson_interval(7, 10) == wilson_interval(7, 10)
    summary = preference_summary(["win", "win", "loss", "tie"])
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert summary["ties"] == 1

    blocked = replacement_gate_report(
        hard_gates={name: True for name in HARD_GATES},
        critical_regressions={name: True for name in CRITICAL_REGRESSIONS},
        primary_quality={
            "meaningful_improvement": False,
            "note": "Only a secondary metric improved.",
        },
        secondary_no_material_regression=True,
        cross_channel_generalization=True,
        holdout_use_count=1,
        canonical_engine_count=1,
    )
    assert blocked["replacement_approved"] is False
    assert blocked["blocking_reasons"] == ["primary_quality_improved"]

    missing_gate = replacement_gate_report(
        hard_gates={name: True for name in HARD_GATES if name != HARD_GATES[0]},
        critical_regressions={name: True for name in CRITICAL_REGRESSIONS},
        primary_quality={"meaningful_improvement": True},
        secondary_no_material_regression=True,
        cross_channel_generalization=True,
        holdout_use_count=1,
        canonical_engine_count=1,
    )
    assert missing_gate["replacement_approved"] is False
    assert missing_gate["checks"]["all_hard_gates"] is False
