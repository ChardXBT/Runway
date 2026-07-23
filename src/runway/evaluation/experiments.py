from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from runway.evaluation.datasets import CaseLabel, DatasetRepository
from runway.evaluation.reports import write_json, write_markdown
from runway.evaluation.scoring import (
    DEFAULT_WEIGHTS,
    aggregate_case_metrics,
    evaluate_selection,
    select_candidate,
)
from runway.intelligence.embeddings import configuration_hash


class ExperimentRunner:
    """Seeded bounded search over persisted candidate pools; never calls a model."""

    protocol_version = "bounded-tuning-v2"

    def __init__(
        self,
        *,
        datasets: DatasetRepository | None = None,
        artifact_root: Path | None = None,
        seed: int = 20260718,
    ):
        self.datasets = datasets or DatasetRepository()
        self.artifact_root = artifact_root or (self.datasets.root.parent / "experiments")
        self.seed = seed

    def tune(self, candidate_artifact: dict[str, Any]) -> dict[str, Any]:
        self._require_splits(candidate_artifact, {"tuning"})
        labels = self.datasets.labels("tuning", purpose="tuning")
        candidate_pool_hash = self._candidate_pool_hash(candidate_artifact)
        configurations = self._bounded_configurations()
        results = [
            self._run_configuration(
                candidate_artifact,
                labels,
                configuration,
                hypothesis=hypothesis,
            )
            for hypothesis, configuration in configurations
        ]
        winner = min(
            results,
            key=lambda row: (
                -float(cast(dict[str, Any], row["metrics"])["objective"]),
                -float(
                    cast(
                        dict[str, float],
                        cast(dict[str, Any], row["configuration"])["weights"],
                    ).get("grounding", 0.0)
                ),
                str(row["configuration_hash"]),
            ),
        )
        winning_objective = float(cast(dict[str, Any], winner["metrics"])["objective"])
        winning_grounding = float(
            cast(
                dict[str, float],
                cast(dict[str, Any], winner["configuration"])["weights"],
            ).get("grounding", 0.0)
        )
        for result in results:
            result["selection_decision"] = (
                "selected"
                if result["configuration_hash"] == winner["configuration_hash"]
                else "rejected"
            )
            if result["selection_decision"] == "rejected":
                objective = float(cast(dict[str, Any], result["metrics"])["objective"])
                grounding = float(
                    cast(
                        dict[str, float],
                        cast(dict[str, Any], result["configuration"])["weights"],
                    ).get("grounding", 0.0)
                )
                if objective < winning_objective:
                    reason = "lower deterministic tuning objective than the selected configuration"
                elif grounding < winning_grounding:
                    reason = (
                        "objective tied; the selected configuration assigns more "
                        "independent weight to the safety-critical grounding verifier"
                    )
                else:
                    reason = (
                        "objective and grounding weight tied; deterministic "
                        "configuration-hash tie-break selected another configuration"
                    )
                result["rejection_reason"] = reason
            self._persist(result)
        summary = {
            "artifact_version": "bounded-tuning-summary-v2",
            "protocol_version": self.protocol_version,
            "dataset_version": candidate_artifact["dataset_version"],
            "seed": self.seed,
            "search_method": "bounded_grid",
            "model_calls": 0,
            "candidate_pool_hash": candidate_pool_hash,
            "configuration_count": len(results),
            "winner": winner,
            "experiments": [
                {
                    "experiment_id": row["experiment_id"],
                    "configuration_hash": row["configuration_hash"],
                    "metrics": row["metrics"],
                    "selection_decision": row["selection_decision"],
                }
                for row in results
            ],
        }
        write_json(self.artifact_root / "tuning-summary.json", summary)
        write_markdown(
            self.artifact_root / "tuning-summary.md",
            "Runway Bounded Tuning",
            summary,
        )
        return summary

    def ablate(
        self,
        candidate_artifact: dict[str, Any],
        *,
        winning_weights: dict[str, float],
    ) -> dict[str, Any]:
        self._require_splits(candidate_artifact, {"tuning"})
        labels = self.datasets.labels("tuning", purpose="tuning")
        components = [
            "grounding",
            "preference",
            "style",
            "novelty",
            "rotation",
            "positive_feedback",
            "negative_feedback_risk",
            "pairing",
        ]
        full = self._score_artifact(
            candidate_artifact,
            labels,
            winning_weights,
        )
        rows = []
        for component in components:
            metrics = self._score_artifact(
                candidate_artifact,
                labels,
                winning_weights,
                disabled_components={component},
            )
            rows.append(
                {
                    "removed_component": component,
                    "metrics": metrics,
                    "objective_delta": round(
                        self._metric_value(metrics, "objective")
                        - self._metric_value(full, "objective"),
                        6,
                    ),
                }
            )
        result = {
            "artifact_version": "canonical-ablation-v1",
            "dataset_version": candidate_artifact["dataset_version"],
            "seed": self.seed,
            "full_engine": full,
            "ablations": rows,
            "model_calls": 0,
        }
        write_json(self.artifact_root / "ablation.json", result)
        write_markdown(
            self.artifact_root / "ablation.md",
            "Runway Intelligence Ablation",
            result,
        )
        return result

    def evaluate_holdout(
        self,
        candidate_artifact: dict[str, Any],
        *,
        winning_weights: dict[str, float],
    ) -> dict[str, Any]:
        self._require_splits(candidate_artifact, {"locked_holdout"})
        marker = self.artifact_root / "HOLDOUT_EVALUATED.json"
        if marker.exists():
            raise RuntimeError(
                "the locked holdout has already been evaluated in this artifact ledger"
            )
        labels = self.datasets.labels(
            "locked_holdout",
            purpose="holdout_release",
        )
        metrics = self._score_artifact(
            candidate_artifact,
            labels,
            winning_weights,
        )
        result = {
            "artifact_version": "locked-holdout-result-v1",
            "dataset_version": candidate_artifact["dataset_version"],
            "evaluated_at": datetime.now(UTC).isoformat(),
            "configuration_hash": configuration_hash(winning_weights),
            "metrics": metrics,
            "use_count": 1,
            "model_calls_during_scoring": 0,
        }
        write_json(marker, result)
        write_markdown(
            self.artifact_root / "holdout-result.md",
            "Runway Locked Holdout",
            result,
        )
        return result

    def compare(
        self,
        baseline: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        baseline_cases = {
            str(row["case_id"]): row
            for row in cast(list[dict[str, Any]], baseline["outputs"]["cases"])
        }
        candidate_cases = {
            str(row["case_id"]): row for row in cast(list[dict[str, Any]], candidate["cases"])
        }
        if set(candidate_cases) - set(baseline_cases):
            raise ValueError("candidate evaluation includes case IDs absent from baseline")
        paired = []
        for case_id in sorted(candidate_cases):
            old = baseline_cases[case_id]
            new = candidate_cases[case_id]
            old_evidence = {
                int(row["post_id"])
                for row in cast(
                    list[dict[str, Any]],
                    cast(dict[str, Any], old.get("retrieval", {})).get(
                        "visual_examples",
                        [],
                    ),
                )
                if row.get("post_id") is not None
            }
            new_evidence = {
                int(row["entity_id"])
                for row in cast(
                    list[dict[str, Any]],
                    cast(dict[str, Any], new["retrieval"])["selected_evidence"],
                )
                if row["entity_type"] == "post"
            }
            union = old_evidence | new_evidence
            paired.append(
                {
                    "case_id": case_id,
                    "old_recommendation": old.get("recommendation"),
                    "new_recommendation": new.get("recommendation"),
                    "old_displayed": old.get("displayed_captions", []),
                    "new_displayed": new.get("displayed_captions", []),
                    "evidence_overlap_jaccard": (
                        round(len(old_evidence & new_evidence) / len(union), 6) if union else 1.0
                    ),
                    "new_role_coverage": cast(
                        dict[str, Any],
                        new["retrieval"],
                    )["role_coverage"],
                    "new_grounding": new["grounding"],
                    "old_abstained": old.get("abstained", False),
                    "new_abstained": new["abstained"],
                    "old_latency_ms": old.get("latency_ms"),
                    "new_latency_ms": new.get("latency_ms"),
                }
            )
        result = {
            "artifact_version": "paired-baseline-comparison-v1",
            "baseline_id": baseline["manifest"]["baseline_id"],
            "candidate_configuration_hash": candidate["configuration_hash"],
            "identical_case_ids": True,
            "case_count": len(paired),
            "baseline_metrics": baseline["metrics"],
            "candidate_metrics": candidate["metrics"],
            "paired_cases": paired,
        }
        write_json(self.artifact_root / "paired-comparison.json", result)
        write_markdown(
            self.artifact_root / "paired-comparison.md",
            "Runway Paired Baseline Comparison",
            result,
        )
        return result

    def _run_configuration(
        self,
        artifact: dict[str, Any],
        labels: list[CaseLabel],
        weights: dict[str, float],
        *,
        hypothesis: str,
    ) -> dict[str, Any]:
        config_hash = configuration_hash(weights)
        candidate_pool_hash = self._candidate_pool_hash(artifact)
        experiment_id = (
            "exp-"
            + hashlib.sha256(
                (
                    f"{artifact['dataset_version']}|{self.seed}|"
                    f"{self.protocol_version}|{candidate_pool_hash}|"
                    f"{config_hash}|{hypothesis}"
                ).encode()
            ).hexdigest()[:12]
        )
        return {
            "artifact_version": "intelligence-experiment-v2",
            "protocol_version": self.protocol_version,
            "experiment_id": experiment_id,
            "parent_experiment_id": None,
            "hypothesis": hypothesis,
            "baseline_commit": "876fe5f814b1a58f0d11b9eaf29f4c508f20595d",
            "candidate_commit": None,
            "datasets": {
                "version": artifact["dataset_version"],
                "split": "tuning",
                "candidate_pool_hash": candidate_pool_hash,
            },
            "configuration": {"weights": weights, "seed": self.seed},
            "configuration_hash": config_hash,
            "components": sorted(weights),
            "model_versions": {"scoring": "deterministic-offline-v1"},
            "prompt_versions": {
                "caption": "captions-v6",
                "annotation": "historical-annotation-v3",
            },
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "metrics": self._score_artifact(artifact, labels, weights),
            "hard_gates": {
                "holdout_not_read": True,
                "paid_calls": 0,
                "publishing_mutations": 0,
            },
            "artifacts": [],
            "selection_decision": "pending",
            "rejection_reason": None,
            "notes": (
                "Automated objective uses deterministic fixture-policy labels, "
                "not human preference."
            ),
        }

    def _score_artifact(
        self,
        artifact: dict[str, Any],
        labels: list[CaseLabel],
        weights: dict[str, float],
        *,
        disabled_components: set[str] | None = None,
    ) -> dict[str, object]:
        by_case = {
            str(row["case_id"]): row for row in cast(list[dict[str, Any]], artifact["cases"])
        }
        rows = []
        for label in labels:
            case = by_case.get(label.case_id)
            if case is None:
                raise LookupError(f"artifact is missing labeled case {label.case_id}")
            selected = select_candidate(
                cast(list[dict[str, Any]], case["generated_pool"]),
                weights,
                disabled_components=disabled_components,
            )
            rows.append(evaluate_selection(selected, label))
        metrics = aggregate_case_metrics(rows)
        metrics["cases"] = rows
        return metrics

    def _persist(self, result: dict[str, Any]) -> None:
        directory = self.artifact_root / str(result["experiment_id"])
        destination = directory / "experiment.json"
        if destination.exists():
            current = json.loads(destination.read_text(encoding="utf-8"))
            comparable_current = {
                key: value
                for key, value in current.items()
                if key not in {"started_at", "completed_at"}
            }
            comparable_result = {
                key: value
                for key, value in result.items()
                if key not in {"started_at", "completed_at"}
            }
            if comparable_current != comparable_result:
                raise FileExistsError(
                    f"refusing to overwrite non-identical experiment {destination}"
                )
            return
        write_json(destination, result)

    @staticmethod
    def _require_splits(
        artifact: dict[str, Any],
        expected: set[str],
    ) -> None:
        observed = set(cast(list[str], artifact.get("splits", [])))
        if observed != expected:
            raise PermissionError(
                f"artifact split mismatch: expected {sorted(expected)}, observed {sorted(observed)}"
            )

    @staticmethod
    def _metric_value(metrics: dict[str, object], key: str) -> float:
        value = metrics[key]
        if not isinstance(value, (int, float)):
            raise TypeError(f"metric {key} is not numeric")
        return float(value)

    @staticmethod
    def _candidate_pool_hash(artifact: dict[str, Any]) -> str:
        cases = [
            {
                "case_id": row.get("case_id"),
                "generated_pool": row.get("generated_pool", []),
            }
            for row in cast(list[dict[str, Any]], artifact.get("cases", []))
        ]
        cases.sort(key=lambda row: str(row["case_id"]))
        return configuration_hash(
            {
                "dataset_version": artifact.get("dataset_version"),
                "splits": artifact.get("splits", []),
                "cases": cases,
            }
        )

    @staticmethod
    def _bounded_configurations() -> list[tuple[str, dict[str, float]]]:
        base = dict(DEFAULT_WEIGHTS)
        grounding = {**base, "grounding": 0.28, "style": 0.07}
        preference = {**base, "preference": 0.26, "novelty": 0.05}
        balanced = {
            **base,
            "grounding": 0.26,
            "preference": 0.23,
            "style": 0.08,
            "novelty": 0.07,
        }
        feedback = {
            **base,
            "positive_feedback": 0.10,
            "negative_feedback_risk": -0.12,
            "style": 0.08,
        }
        pairing = {**base, "pairing": 0.14, "style": 0.07}
        return [
            ("canonical hand-designed starting point", base),
            ("increase independent grounding weight", grounding),
            ("increase learned creator-preference weight", preference),
            ("balance grounding, preference, and novelty", balanced),
            ("strengthen explicit positive and negative feedback", feedback),
            ("strengthen complete image-caption pairing", pairing),
        ]
