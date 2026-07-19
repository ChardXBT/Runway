from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from runway.config import Settings

EvaluationSplit = Literal[
    "development",
    "tuning",
    "locked_holdout",
    "blind_creator_preference",
    "cross_channel_generalization",
    "critical_regression",
]


class CaseLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    split: EvaluationSplit
    cluster_id: str
    preferred_structures: list[str]
    expected_terms: list[str]
    forbidden_terms: list[str]
    label_source: str


class BlindPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    split: Literal["blind_creator_preference"]
    cluster_id: str
    left: str
    right: str
    preferred: Literal["left", "right", "tie"]


class CanonicalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    baseline_id: str
    case_labels: list[CaseLabel]
    blind_preferences: list[BlindPreference]
    critical_regressions: list[str]


class SharedImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: str
    visible_facts: list[str]


class ChannelFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    name: str
    language: str
    locale: str
    question_first: bool
    preferred_structures: list[str]
    history: list[str] = Field(min_length=3)
    topics: list[str]
    expected_structure: str
    expected_terms: list[str]


class GeneralizationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    shared_image: SharedImage
    channels: list[ChannelFixture] = Field(min_length=5)


class DatasetRepository:
    """Loads labels through a split-aware boundary so tuning cannot read holdout labels."""

    def __init__(self, root: Path | None = None):
        project_root = Settings().project_root
        self.root = root or (project_root / "benchmarks" / "intelligence" / "datasets")

    def canonical(self) -> CanonicalDataset:
        return CanonicalDataset.model_validate_json(
            (self.root / "canonical-v1.json").read_text(encoding="utf-8")
        )

    def generalization(self) -> GeneralizationDataset:
        return GeneralizationDataset.model_validate_json(
            (self.root / "generalization-v1.json").read_text(encoding="utf-8")
        )

    def labels(
        self,
        split: EvaluationSplit,
        *,
        purpose: Literal[
            "development",
            "tuning",
            "holdout_release",
            "reporting",
        ],
    ) -> list[CaseLabel]:
        if split == "locked_holdout" and purpose != "holdout_release":
            raise PermissionError(
                "locked holdout labels are unavailable outside the one-time release run"
            )
        if purpose == "tuning" and split != "tuning":
            raise PermissionError("tuning may read only tuning labels")
        if purpose == "development" and split != "development":
            raise PermissionError("development diagnostics may read only development labels")
        return [label for label in self.canonical().case_labels if label.split == split]

    def validate_split_integrity(self) -> dict[str, object]:
        canonical = self.canonical()
        clusters: dict[str, set[str]] = {}
        for label in canonical.case_labels:
            clusters.setdefault(label.cluster_id, set()).add(label.split)
        for preference in canonical.blind_preferences:
            clusters.setdefault(preference.cluster_id, set()).add(preference.split)
        contaminated = {
            cluster: sorted(splits) for cluster, splits in clusters.items() if len(splits) > 1
        }
        case_ids = [label.case_id for label in canonical.case_labels] + [
            preference.case_id for preference in canonical.blind_preferences
        ]
        duplicate_case_ids = sorted(
            {case_id for case_id in case_ids if case_ids.count(case_id) > 1}
        )
        if contaminated or duplicate_case_ids:
            raise ValueError(
                "dataset split integrity failed: "
                f"clusters={contaminated}, duplicate_case_ids={duplicate_case_ids}"
            )
        return {
            "dataset_version": canonical.dataset_version,
            "case_count": len(case_ids),
            "cluster_count": len(clusters),
            "cross_split_clusters": 0,
            "duplicate_case_ids": 0,
            "passed": True,
        }

    def randomized_blind_preferences(
        self,
        *,
        seed: int,
    ) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for preference in self.canonical().blind_preferences:
            case_seed = int(
                hashlib.sha256(f"{seed}|{preference.case_id}".encode()).hexdigest()[:16],
                16,
            )
            swap = bool(random.Random(case_seed).getrandbits(1))
            values.append(
                {
                    "case_id": preference.case_id,
                    "first": preference.right if swap else preference.left,
                    "second": preference.left if swap else preference.right,
                    "order_token": "swapped" if swap else "original",
                }
            )
        return values

    def export_blind_review(self, destination: Path, *, seed: int) -> Path:
        payload = {
            "dataset_version": self.canonical().dataset_version,
            "seed": seed,
            "cases": self.randomized_blind_preferences(seed=seed),
            "instructions": "Record first, second, or tie for each case_id.",
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return destination
