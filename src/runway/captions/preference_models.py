from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select

from runway.captions.feature_snapshots import (
    FEATURE_SCHEMA_VERSION,
    caption_feature_values,
)
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    IntelligenceActivation,
    PairwisePreference,
    PreferenceDataset,
    PreferenceDatasetItem,
    PreferenceModelVersion,
    utcnow,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import configuration_hash

PREFERENCE_TARGETS = ("caption", "image", "pairing")
ENGINEERING_MINIMUM_LABELS = {
    "caption": 8,
    "image": 8,
    "pairing": 8,
}
PRODUCT_CHALLENGER_MINIMUM_LABELS = {
    "caption": 100,
    "image": 100,
    "pairing": 100,
}
# Backward-compatible public name; this is explicitly an algorithm smoke floor,
# not a product-quality threshold.
MINIMUM_LABELS = ENGINEERING_MINIMUM_LABELS
REQUIRED_PREFERENCE_ACTIVATION_GATES = frozenset(
    {
        "artifact_integrity",
        "schema_compatibility",
        "channel_isolation",
        "quality_non_regression",
        "calibration_truthful",
        "product_label_threshold",
        "offline_only",
    }
)


class InsufficientPreferenceData(ValueError):
    pass


@dataclass(frozen=True)
class PersistedModelScore:
    score: float
    calibrated: bool
    sample_count: int
    model_version_id: int
    reason: str


def _json_object(value: str, *, label: str) -> dict[str, object]:
    payload: object = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is not a JSON object")
    return payload


def _feature_mapping(snapshot_json: str) -> dict[str, float]:
    snapshot = _json_object(snapshot_json, label="feature snapshot")
    raw = snapshot.get("features")
    if not isinstance(raw, dict):
        raise ValueError("feature snapshot has no features")
    result: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError("feature names must be text")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"feature {key!r} is not numeric")
        result[key] = float(value)
    if not result:
        raise ValueError("feature snapshot is empty")
    return result


def _required_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is not an integer")
    return value


def _required_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is not numeric")
    return float(value)


class PreferenceDatasetService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings

    def build(
        self,
        target: str,
        *,
        seed: int = 20260718,
        feature_schema_version: str = FEATURE_SCHEMA_VERSION,
        label_sources: frozenset[str] = frozenset({"human"}),
        engineering_test: bool = False,
    ) -> dict[str, object]:
        if target not in PREFERENCE_TARGETS:
            raise ValueError(f"unsupported preference target: {target}")
        if label_sources != frozenset({"human"}) and not (
            engineering_test
            and self.settings.agent_runtime == "mock"
            and not self.settings.publishing_enabled
        ):
            raise ValueError(
                "non-human preference datasets are allowed only in an explicit offline "
                "engineering test"
            )
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            rows = session.scalars(
                select(PairwisePreference)
                .where(
                    PairwisePreference.channel_id == channel.id,
                    PairwisePreference.target == target,
                    PairwisePreference.label_source.in_(label_sources),
                    PairwisePreference.learning_split.in_(("development", "tuning")),
                    PairwisePreference.feature_schema_version == feature_schema_version,
                    PairwisePreference.feature_snapshot_hash.is_not(None),
                )
                .order_by(PairwisePreference.id)
            ).all()
            channel_id = channel.id
        planned: list[dict[str, object]] = []
        for row in rows:
            preferred = _feature_mapping(row.preferred_features_json)
            dispreferred = _feature_mapping(row.dispreferred_features_json)
            if set(preferred) != set(dispreferred):
                raise ValueError(f"pairwise preference {row.id} has incompatible feature snapshots")
            group_key = row.group_key or (
                f"proposal:{row.proposal_id}"
                if row.proposal_id is not None
                else f"preference:{row.id}"
            )
            split = self._split(group_key, seed)
            planned.append(
                {
                    "pairwise_preference_id": row.id,
                    "split": split,
                    "group_key": group_key,
                    "preferred_features_json": row.preferred_features_json,
                    "dispreferred_features_json": row.dispreferred_features_json,
                    "strength": row.strength,
                    "snapshot_hash": row.feature_snapshot_hash,
                }
            )
        configuration = {
            "version": "preference-dataset-v1",
            "target": target,
            "seed": seed,
            "feature_schema_version": feature_schema_version,
            "label_sources": sorted(label_sources),
            "engineering_test": engineering_test,
            "split_policy": {
                "train": [0, 69],
                "validation": [70, 84],
                "test": [85, 99],
                "group_protected": True,
            },
        }
        config_hash = configuration_hash(configuration)
        content_payload = {
            "channel_id": channel_id,
            "target": target,
            "configuration_hash": config_hash,
            "rows": [
                {
                    "pairwise_preference_id": row["pairwise_preference_id"],
                    "split": row["split"],
                    "group_key": row["group_key"],
                    "snapshot_hash": row["snapshot_hash"],
                    "strength": row["strength"],
                }
                for row in planned
            ],
        }
        content_digest = configuration_hash(content_payload)
        dataset_id = f"prefds-{content_digest[:20]}"
        split_counts = Counter(str(row["split"]) for row in planned)
        with self.database.session() as session:
            existing = session.get(PreferenceDataset, dataset_id)
            if existing is None:
                dataset = PreferenceDataset(
                    dataset_id=dataset_id,
                    channel_id=channel_id,
                    target=target,
                    status="frozen",
                    feature_schema_version=feature_schema_version,
                    taxonomy_version="caption-taxonomy-v1",
                    split_seed=seed,
                    configuration_json=json.dumps(
                        configuration,
                        sort_keys=True,
                    ),
                    configuration_hash=config_hash,
                    content_hash=content_digest,
                    row_count=len(planned),
                    split_counts_json=json.dumps(
                        dict(split_counts),
                        sort_keys=True,
                    ),
                )
                session.add(dataset)
                session.flush()
                items: list[PreferenceDatasetItem] = []
                for index, planned_row in enumerate(planned, start=1):
                    items.append(
                        PreferenceDatasetItem(
                            dataset_id=dataset_id,
                            pairwise_preference_id=_required_int(
                                planned_row["pairwise_preference_id"],
                                label="pairwise preference id",
                            ),
                            split=str(planned_row["split"]),
                            group_key=str(planned_row["group_key"]),
                            position=index,
                            preferred_features_json=str(planned_row["preferred_features_json"]),
                            dispreferred_features_json=str(
                                planned_row["dispreferred_features_json"]
                            ),
                            strength=_required_float(
                                planned_row["strength"],
                                label="preference strength",
                            ),
                            snapshot_hash=str(planned_row["snapshot_hash"]),
                        )
                    )
                session.add_all(items)
                created = True
            else:
                created = False
        return {
            "dataset_id": dataset_id,
            "target": target,
            "row_count": len(planned),
            "split_counts": dict(split_counts),
            "configuration_hash": config_hash,
            "content_hash": content_digest,
            "created": created,
        }

    @staticmethod
    def _split(group_key: str, seed: int) -> str:
        digest = hashlib.sha256(f"{seed}:{group_key}".encode()).digest()
        bucket = int.from_bytes(digest[:8], "big") % 100
        if bucket < 70:
            return "train"
        if bucket < 85:
            return "validation"
        return "test"


class PreferenceModelService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings
        self.datasets = PreferenceDatasetService(database, self.settings)

    def train(
        self,
        target: str,
        *,
        dataset_id: str | None = None,
        seed: int = 20260718,
        epochs: int = 200,
        learning_rate: float = 0.05,
        regularization: float = 0.002,
        minimum_label_count: int | None = None,
    ) -> dict[str, object]:
        if target not in PREFERENCE_TARGETS:
            raise ValueError(f"unsupported preference target: {target}")
        if epochs < 1 or epochs > 10_000:
            raise ValueError("epochs must be between 1 and 10000")
        minimum = minimum_label_count or ENGINEERING_MINIMUM_LABELS[target]
        if dataset_id is None:
            dataset_id = str(self.datasets.build(target, seed=seed)["dataset_id"])
        with self.database.session() as session:
            dataset = session.get(PreferenceDataset, dataset_id)
            if dataset is None or dataset.target != target or dataset.status != "frozen":
                raise ValueError("preference dataset is missing, mutable, or target-mismatched")
            items = session.scalars(
                select(PreferenceDatasetItem)
                .where(PreferenceDatasetItem.dataset_id == dataset_id)
                .order_by(PreferenceDatasetItem.position)
            ).all()
            channel_id = dataset.channel_id
            schema_version = dataset.feature_schema_version
        training_configuration = {
            "algorithm": "pairwise_logistic",
            "version": "pairwise-logistic-v1",
            "target": target,
            "dataset_id": dataset_id,
            "seed": seed,
            "epochs": epochs,
            "learning_rate": learning_rate,
            "regularization": regularization,
            "minimum_label_count": minimum,
        }
        config_hash = configuration_hash(training_configuration)
        if len(items) < minimum:
            model_id = self._persist_insufficient(
                channel_id=channel_id,
                target=target,
                dataset_id=dataset_id,
                feature_schema_version=schema_version,
                label_count=len(items),
                minimum_label_count=minimum,
                training_configuration=training_configuration,
                configuration_hash_value=config_hash,
                reason="minimum target-specific label threshold was not met",
            )
            raise InsufficientPreferenceData(
                f"{target} requires {minimum} labels; found {len(items)} (model record {model_id})"
            )

        parsed: list[tuple[str, np.ndarray, np.ndarray, float]] = []
        feature_names: list[str] | None = None
        for item in items:
            preferred = _feature_mapping(item.preferred_features_json)
            dispreferred = _feature_mapping(item.dispreferred_features_json)
            names = sorted(preferred)
            if feature_names is None:
                feature_names = names
            if names != feature_names or sorted(dispreferred) != feature_names:
                raise ValueError("preference dataset mixes incompatible feature schemas")
            parsed.append(
                (
                    item.split,
                    np.asarray(
                        [preferred[name] for name in feature_names],
                        dtype=np.float64,
                    ),
                    np.asarray(
                        [dispreferred[name] for name in feature_names],
                        dtype=np.float64,
                    ),
                    item.strength,
                )
            )
        assert feature_names is not None
        train_rows = [row for row in parsed if row[0] == "train"]
        if not train_rows:
            model_id = self._persist_insufficient(
                channel_id=channel_id,
                target=target,
                dataset_id=dataset_id,
                feature_schema_version=schema_version,
                label_count=len(items),
                minimum_label_count=minimum,
                training_configuration=training_configuration,
                configuration_hash_value=config_hash,
                reason="group-protected split has no training rows",
            )
            raise InsufficientPreferenceData(
                f"group-protected split has no training rows (model record {model_id})"
            )
        weights = self._fit(
            train_rows,
            dimensions=len(feature_names),
            epochs=epochs,
            learning_rate=learning_rate,
            regularization=regularization,
        )
        metrics = {
            split: self._metrics(
                [row for row in parsed if row[0] == split],
                weights,
            )
            for split in ("train", "validation", "test")
        }
        held_out = [row for row in parsed if row[0] in {"validation", "test"}]
        calibration = self._calibration(held_out, weights)
        parameters = {
            "weights": [float(value) for value in weights],
            "feature_names": feature_names,
        }
        artifact_payload = {
            "dataset_id": dataset_id,
            "configuration_hash": config_hash,
            "parameters": parameters,
            "metrics": metrics,
            "calibration": calibration,
        }
        artifact_hash = configuration_hash(artifact_payload)
        with self.database.session() as session:
            existing = session.scalar(
                select(PreferenceModelVersion)
                .where(
                    PreferenceModelVersion.channel_id == channel_id,
                    PreferenceModelVersion.target == target,
                    PreferenceModelVersion.artifact_hash == artifact_hash,
                )
                .limit(1)
            )
            if existing is None:
                model = PreferenceModelVersion(
                    channel_id=channel_id,
                    target=target,
                    algorithm="pairwise_logistic",
                    status="trained",
                    dataset_id=dataset_id,
                    feature_schema_version=schema_version,
                    feature_names_json=json.dumps(feature_names),
                    parameters_json=json.dumps(parameters, sort_keys=True),
                    training_configuration_json=json.dumps(
                        training_configuration,
                        sort_keys=True,
                    ),
                    metrics_json=json.dumps(metrics, sort_keys=True),
                    calibration_json=json.dumps(calibration, sort_keys=True),
                    label_count=len(items),
                    minimum_label_count=minimum,
                    configuration_hash=config_hash,
                    artifact_hash=artifact_hash,
                    active=False,
                )
                session.add(model)
                session.flush()
                model_id = model.id
                created = True
            else:
                model_id = existing.id
                created = False
        return {
            "model_version_id": model_id,
            "target": target,
            "dataset_id": dataset_id,
            "label_count": len(items),
            "feature_names": feature_names,
            "metrics": metrics,
            "calibration": calibration,
            "artifact_hash": artifact_hash,
            "created": created,
        }

    def activate(
        self,
        model_version_id: int,
        *,
        reason: str,
        gate_results: dict[str, bool],
        activation_tier: str = "product",
    ) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("activation reason is required")
        failed_gates = sorted(
            gate
            for gate in REQUIRED_PREFERENCE_ACTIVATION_GATES
            if gate_results.get(gate) is not True
        )
        if failed_gates:
            raise ValueError(
                "preference-model activation gates failed or are missing: "
                + ", ".join(failed_gates)
            )
        with self.database.session() as session:
            target = session.get(PreferenceModelVersion, model_version_id)
            if (
                target is None
                or target.status != "trained"
                or target.label_count < target.minimum_label_count
            ):
                raise ValueError("only a trained, threshold-qualified model can activate")
            if target.feature_schema_version != FEATURE_SCHEMA_VERSION:
                raise ValueError("preference-model feature schema is incompatible with production")
            dataset = session.get(PreferenceDataset, target.dataset_id)
            if (
                dataset is None
                or dataset.channel_id != target.channel_id
                or dataset.target != target.target
                or dataset.status != "frozen"
                or dataset.feature_schema_version != target.feature_schema_version
            ):
                raise ValueError("preference-model dataset is missing, mutable, or incompatible")
            dataset_configuration = _json_object(
                dataset.configuration_json,
                label="preference dataset configuration",
            )
            product_minimum = PRODUCT_CHALLENGER_MINIMUM_LABELS[target.target]
            if activation_tier == "product":
                if target.label_count < product_minimum:
                    raise ValueError(
                        f"product activation requires {product_minimum} genuine human "
                        f"{target.target} labels; found {target.label_count}"
                    )
                if dataset_configuration.get("label_sources") != ["human"]:
                    raise ValueError("product activation requires a human-only frozen dataset")
            elif activation_tier == "engineering_test":
                if self.settings.agent_runtime != "mock" or self.settings.publishing_enabled:
                    raise ValueError(
                        "engineering-test activation requires mock runtime and publishing disabled"
                    )
            else:
                raise ValueError("activation_tier must be product or engineering_test")
            parameters = _json_object(
                target.parameters_json,
                label="preference model parameters",
            )
            metrics = _json_object(
                target.metrics_json,
                label="preference model metrics",
            )
            calibration = _json_object(
                target.calibration_json,
                label="preference model calibration",
            )
            feature_names: object = json.loads(target.feature_names_json)
            if (
                not isinstance(feature_names, list)
                or not feature_names
                or not all(isinstance(name, str) for name in feature_names)
                or parameters.get("feature_names") != feature_names
            ):
                raise ValueError("preference-model feature identity is malformed")
            weights = parameters.get("weights")
            if (
                not isinstance(weights, list)
                or len(weights) != len(feature_names)
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in weights
                )
            ):
                raise ValueError("preference-model parameters failed validation")
            if set(metrics) != {"train", "validation", "test"}:
                raise ValueError("preference-model evaluation metrics are incomplete")
            expected_artifact_hash = configuration_hash(
                {
                    "dataset_id": target.dataset_id,
                    "configuration_hash": target.configuration_hash,
                    "parameters": parameters,
                    "metrics": metrics,
                    "calibration": calibration,
                }
            )
            if expected_artifact_hash != target.artifact_hash:
                raise ValueError("preference-model artifact hash does not match")
            active_rows = session.scalars(
                select(PreferenceModelVersion)
                .where(
                    PreferenceModelVersion.channel_id == target.channel_id,
                    PreferenceModelVersion.target == target.target,
                    PreferenceModelVersion.active.is_(True),
                    PreferenceModelVersion.id != target.id,
                )
                .order_by(
                    PreferenceModelVersion.activated_at.desc(),
                    PreferenceModelVersion.id.desc(),
                )
            ).all()
            if len(active_rows) > 1:
                raise RuntimeError("multiple active preference models already exist")
            previous = active_rows[0] if active_rows else None
            if previous is not None:
                previous.active = False
                previous.status = "superseded"
                previous.superseded_at = utcnow()
                target.parent_model_id = previous.id
            target.active = True
            target.status = "active"
            target.activated_at = utcnow()
            target.superseded_at = None
            target.error_summary = None
            session.add(
                IntelligenceActivation(
                    channel_id=target.channel_id,
                    resource_type="preference_model",
                    target=target.target,
                    action="activate",
                    resource_id=str(target.id),
                    previous_resource_id=(str(previous.id) if previous is not None else None),
                    reason=reason.strip(),
                    gate_results_json=json.dumps(
                        {**gate_results, "activation_tier": activation_tier},
                        sort_keys=True,
                    ),
                )
            )
        return self.inspect(model_version_id)

    def rollback(
        self,
        model_version_id: int,
        *,
        reason: str,
    ) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        with self.database.session() as session:
            current = session.get(PreferenceModelVersion, model_version_id)
            if current is None or not current.active or current.status != "active":
                raise ValueError("preference model is not active")
            if current.parent_model_id is None:
                raise ValueError("preference model has no prior active version")
            target = session.get(
                PreferenceModelVersion,
                current.parent_model_id,
            )
            if (
                target is None
                or target.target != current.target
                or target.channel_id != current.channel_id
                or target.label_count < target.minimum_label_count
            ):
                raise ValueError("prior preference model is not rollback-eligible")
            current.active = False
            current.status = "rolled_back"
            current.superseded_at = utcnow()
            target.active = True
            target.status = "active"
            target.activated_at = utcnow()
            target.superseded_at = None
            session.add(
                IntelligenceActivation(
                    channel_id=current.channel_id,
                    resource_type="preference_model",
                    target=current.target,
                    action="rollback",
                    resource_id=str(target.id),
                    previous_resource_id=str(current.id),
                    reason=reason.strip(),
                    gate_results_json="{}",
                )
            )
        return self.inspect(target.id)

    def score_caption(
        self,
        *,
        channel_id: int,
        text: str,
        components: dict[str, float],
    ) -> PersistedModelScore | None:
        features = caption_feature_values(text, components)
        return self.score_features(
            channel_id=channel_id,
            target="caption",
            features=features,
        )

    def score_features(
        self,
        *,
        channel_id: int,
        target: str,
        features: dict[str, float],
    ) -> PersistedModelScore | None:
        with self.database.session() as session:
            rows = session.scalars(
                select(PreferenceModelVersion)
                .where(
                    PreferenceModelVersion.channel_id == channel_id,
                    PreferenceModelVersion.target == target,
                    PreferenceModelVersion.active.is_(True),
                    PreferenceModelVersion.status == "active",
                )
                .order_by(PreferenceModelVersion.id)
            ).all()
        if not rows:
            return None
        if len(rows) > 1:
            raise RuntimeError("multiple active preference models violate scoring")
        model = rows[0]
        parameters = _json_object(
            model.parameters_json,
            label="preference model parameters",
        )
        names_raw = parameters.get("feature_names")
        weights_raw = parameters.get("weights")
        if not isinstance(names_raw, list) or not all(isinstance(name, str) for name in names_raw):
            raise ValueError("preference model feature names are malformed")
        if not isinstance(weights_raw, list) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in weights_raw
        ):
            raise ValueError("preference model weights are malformed")
        names = [str(name) for name in names_raw]
        weights = np.asarray(weights_raw, dtype=np.float64)
        if len(names) != len(weights):
            raise ValueError("preference model dimensions are inconsistent")
        vector = np.asarray(
            [float(features.get(name, 0.0)) for name in names],
            dtype=np.float64,
        )
        probability = self._sigmoid(float(np.dot(weights, vector)))
        calibration = _json_object(
            model.calibration_json,
            label="preference calibration",
        )
        calibrated = bool(calibration.get("calibrated", False))
        return PersistedModelScore(
            score=probability,
            calibrated=calibrated,
            sample_count=model.label_count,
            model_version_id=model.id,
            reason=(
                "persisted pairwise logistic preference model"
                + ("" if calibrated else "; probability is uncalibrated")
            ),
        )

    def inspect(self, model_version_id: int) -> dict[str, object]:
        with self.database.session() as session:
            row = session.get(PreferenceModelVersion, model_version_id)
            if row is None:
                raise LookupError(f"preference model {model_version_id} was not found")
            return {
                "model_version_id": row.id,
                "channel_id": row.channel_id,
                "target": row.target,
                "algorithm": row.algorithm,
                "status": row.status,
                "active": row.active,
                "dataset_id": row.dataset_id,
                "parent_model_id": row.parent_model_id,
                "feature_schema_version": row.feature_schema_version,
                "feature_names": json.loads(row.feature_names_json),
                "parameters": json.loads(row.parameters_json),
                "training_configuration": json.loads(row.training_configuration_json),
                "metrics": json.loads(row.metrics_json),
                "calibration": json.loads(row.calibration_json),
                "label_count": row.label_count,
                "minimum_label_count": row.minimum_label_count,
                "configuration_hash": row.configuration_hash,
                "artifact_hash": row.artifact_hash,
                "error_summary": row.error_summary,
            }

    def _persist_insufficient(
        self,
        *,
        channel_id: int,
        target: str,
        dataset_id: str,
        feature_schema_version: str,
        label_count: int,
        minimum_label_count: int,
        training_configuration: dict[str, object],
        configuration_hash_value: str,
        reason: str,
    ) -> int:
        artifact_hash = configuration_hash(
            {
                "status": "insufficient_data",
                "dataset_id": dataset_id,
                "configuration_hash": configuration_hash_value,
                "reason": reason,
            }
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(PreferenceModelVersion)
                .where(
                    PreferenceModelVersion.channel_id == channel_id,
                    PreferenceModelVersion.target == target,
                    PreferenceModelVersion.artifact_hash == artifact_hash,
                )
                .limit(1)
            )
            if existing is not None:
                return existing.id
            model = PreferenceModelVersion(
                channel_id=channel_id,
                target=target,
                algorithm="pairwise_logistic",
                status="insufficient_data",
                dataset_id=dataset_id,
                feature_schema_version=feature_schema_version,
                feature_names_json="[]",
                parameters_json="{}",
                training_configuration_json=json.dumps(
                    training_configuration,
                    sort_keys=True,
                ),
                metrics_json="{}",
                calibration_json=json.dumps(
                    {
                        "calibrated": False,
                        "reason": "insufficient labels",
                    },
                    sort_keys=True,
                ),
                label_count=label_count,
                minimum_label_count=minimum_label_count,
                configuration_hash=configuration_hash_value,
                artifact_hash=artifact_hash,
                active=False,
                error_summary=reason,
            )
            session.add(model)
            session.flush()
            return model.id

    @staticmethod
    def _fit(
        rows: list[tuple[str, np.ndarray, np.ndarray, float]],
        *,
        dimensions: int,
        epochs: int,
        learning_rate: float,
        regularization: float,
    ) -> np.ndarray:
        weights = np.zeros(dimensions, dtype=np.float64)
        rate = learning_rate
        for _epoch in range(epochs):
            for _split, preferred, dispreferred, strength in rows:
                difference = preferred - dispreferred
                probability = PreferenceModelService._sigmoid(float(np.dot(weights, difference)))
                gradient = strength * (1.0 - probability) * difference - regularization * weights
                weights += rate * gradient
            rate *= 0.99
        return weights

    @staticmethod
    def _metrics(
        rows: list[tuple[str, np.ndarray, np.ndarray, float]],
        weights: np.ndarray,
    ) -> dict[str, object]:
        if not rows:
            return {
                "count": 0,
                "pairwise_accuracy": None,
                "log_loss": None,
            }
        probabilities = [
            PreferenceModelService._sigmoid(float(np.dot(weights, preferred - dispreferred)))
            for _split, preferred, dispreferred, _strength in rows
        ]
        return {
            "count": len(rows),
            "pairwise_accuracy": sum(value >= 0.5 for value in probabilities) / len(probabilities),
            "log_loss": -sum(math.log(max(value, 1e-12)) for value in probabilities)
            / len(probabilities),
        }

    @staticmethod
    def _calibration(
        rows: list[tuple[str, np.ndarray, np.ndarray, float]],
        weights: np.ndarray,
    ) -> dict[str, object]:
        if len(rows) < 30:
            return {
                "calibrated": False,
                "held_out_pair_count": len(rows),
                "reason": "at least 30 held-out pairs are required",
            }
        predictions: list[tuple[float, int]] = []
        for _split, preferred, dispreferred, _strength in rows:
            probability = PreferenceModelService._sigmoid(
                float(np.dot(weights, preferred - dispreferred))
            )
            predictions.extend(((probability, 1), (1.0 - probability, 0)))
        brier = sum((probability - label) ** 2 for probability, label in predictions) / len(
            predictions
        )
        return {
            "calibrated": False,
            "calibration_evaluated": True,
            "method": "held-out symmetric pair calibration audit",
            "held_out_pair_count": len(rows),
            "brier_score": brier,
            "reason": "no independent calibration set was available to fit a calibrator",
        }

    @staticmethod
    def _sigmoid(value: float) -> float:
        clipped = max(-30.0, min(30.0, value))
        return 1.0 / (1.0 + math.exp(-clipped))
