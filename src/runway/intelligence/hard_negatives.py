from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import combinations

from sqlalchemy import select

from runway.captions.feature_snapshots import FEATURE_SCHEMA_VERSION
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CaptionCandidateRecord,
    CaptionSlate,
    IntelligenceRetrievalRun,
    PairwisePreference,
)
from runway.db.repositories import audit, get_channel
from runway.intelligence.embeddings import configuration_hash


class HardNegativeMiningService:
    """Mine policy-labelled contrasts while keeping creator truth strictly separate."""

    version = "hard-negative-mining-v1"

    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings

    def mine(self, *, limit: int = 500) -> dict[str, object]:
        if limit < 1 or limit > 10_000:
            raise ValueError("hard-negative mining limit must be between 1 and 10000")
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            slates = session.scalars(
                select(CaptionSlate)
                .where(CaptionSlate.channel_id == channel_id)
                .order_by(CaptionSlate.id)
            ).all()
            rows = session.scalars(
                select(CaptionCandidateRecord)
                .where(
                    CaptionCandidateRecord.channel_id == channel_id,
                    CaptionCandidateRecord.feature_schema_version == FEATURE_SCHEMA_VERSION,
                    CaptionCandidateRecord.feature_snapshot_hash.is_not(None),
                )
                .order_by(CaptionCandidateRecord.caption_slate_id, CaptionCandidateRecord.id)
            ).all()
        by_slate: dict[int, list[CaptionCandidateRecord]] = {}
        for row in rows:
            by_slate.setdefault(row.caption_slate_id, []).append(row)
        slate_by_id = {slate.id: slate for slate in slates}
        planned: list[dict[str, object]] = []
        for slate_id, candidates in by_slate.items():
            slate = slate_by_id.get(slate_id)
            if slate is None:
                continue
            for first, second in combinations(candidates, 2):
                contrast = self._contrast(first, second)
                if contrast is None:
                    continue
                preferred, dispreferred, target, category, strength = contrast
                idempotency_key = self._key(
                    channel_id,
                    slate_id,
                    preferred.id,
                    dispreferred.id,
                    target,
                    category,
                )
                planned.append(
                    {
                        "slate": slate,
                        "preferred": preferred,
                        "dispreferred": dispreferred,
                        "target": target,
                        "category": category,
                        "strength": strength,
                        "idempotency_key": idempotency_key,
                    }
                )
                if len(planned) >= limit:
                    break
            if len(planned) >= limit:
                break

        created = 0
        categories: Counter[str] = Counter()
        targets: Counter[str] = Counter()
        with self.database.session() as session:
            existing = set(
                session.scalars(
                    select(PairwisePreference.idempotency_key).where(
                        PairwisePreference.idempotency_key.in_(
                            [str(item["idempotency_key"]) for item in planned]
                        )
                    )
                ).all()
            )
            for item in planned:
                key = str(item["idempotency_key"])
                if key in existing:
                    continue
                persisted_slate = item["slate"]
                persisted_preferred = item["preferred"]
                persisted_dispreferred = item["dispreferred"]
                if not isinstance(persisted_slate, CaptionSlate):
                    raise TypeError("hard-negative slate payload is malformed")
                if not isinstance(persisted_preferred, CaptionCandidateRecord) or not isinstance(
                    persisted_dispreferred, CaptionCandidateRecord
                ):
                    raise TypeError("hard-negative candidate payload is malformed")
                target = str(item["target"])
                category = str(item["category"])
                strength = self._number(item["strength"])
                retrieval = (
                    session.get(
                        IntelligenceRetrievalRun,
                        persisted_slate.retrieval_run_id,
                    )
                    if persisted_slate.retrieval_run_id is not None
                    else None
                )
                snapshot_hash = hashlib.sha256(
                    (
                        f"{persisted_preferred.feature_snapshot_hash}:"
                        f"{persisted_dispreferred.feature_snapshot_hash}:{category}"
                    ).encode()
                ).hexdigest()
                session.add(
                    PairwisePreference(
                        channel_id=channel_id,
                        proposal_id=persisted_slate.proposal_id,
                        candidate_image_id=persisted_slate.candidate_image_id,
                        preferred_candidate_id=persisted_preferred.id,
                        preferred_text=persisted_preferred.text,
                        dispreferred_candidate_id=persisted_dispreferred.id,
                        dispreferred_text=persisted_dispreferred.text,
                        preference_source="hard_negative_mining",
                        label_source="policy",
                        strength=strength,
                        reason_codes_json=json.dumps([category]),
                        policy_version=self._policy_version(persisted_slate.editorial_brief_json),
                        experiment_id=None,
                        target=target,
                        source_proposal_event_id=None,
                        source_exposure_id=None,
                        source_event_key=f"hard-negative:{key[:24]}",
                        derivation_version=self.version,
                        idempotency_key=key,
                        preferred_features_json=persisted_preferred.feature_snapshot_json,
                        dispreferred_features_json=persisted_dispreferred.feature_snapshot_json,
                        context_snapshot_json=json.dumps(
                            {
                                "caption_slate_id": persisted_slate.id,
                                "category": category,
                                "label_truth": "policy_not_human",
                                "eligible_for_product_creator_truth": False,
                            },
                            sort_keys=True,
                        ),
                        feature_schema_version=FEATURE_SCHEMA_VERSION,
                        feature_snapshot_hash=snapshot_hash,
                        group_key=f"caption-slate:{persisted_slate.id}",
                        taxonomy_version=persisted_preferred.taxonomy_version,
                        verifier_version=persisted_preferred.verifier_version,
                        style_profile_version=(
                            retrieval.profile_version if retrieval is not None else None
                        ),
                        representation_sets_json=(
                            retrieval.representation_sets_json if retrieval is not None else "{}"
                        ),
                        retrieval_configuration_hash=(
                            retrieval.configuration_hash if retrieval is not None else None
                        ),
                        ranker_configuration_hash=configuration_hash(
                            {
                                "source": self.version,
                                "caption_slate_configuration": (persisted_slate.configuration_hash),
                            }
                        ),
                        learning_split="development",
                        source_study_response_id=None,
                    )
                )
                created += 1
                categories[category] += 1
                targets[target] += 1
            audit(
                session,
                "hard_negatives_mined",
                "channel",
                channel_id,
                {
                    "version": self.version,
                    "planned": len(planned),
                    "created": created,
                    "label_source": "policy",
                    "human_labels_created": 0,
                    "categories": dict(categories),
                    "targets": dict(targets),
                },
            )
        return {
            "version": self.version,
            "planned": len(planned),
            "created": created,
            "label_source": "policy",
            "human_labels_created": 0,
            "product_training_eligible": False,
            "categories": dict(sorted(categories.items())),
            "targets": dict(sorted(targets.items())),
            "active_learning_priority_signals": [
                "close_scores",
                "verifier_near_threshold",
                "deterministic_neural_disagreement",
                "abstention",
                "new_content_mode",
            ],
        }

    def _contrast(
        self,
        first: CaptionCandidateRecord,
        second: CaptionCandidateRecord,
    ) -> (
        tuple[
            CaptionCandidateRecord,
            CaptionCandidateRecord,
            str,
            str,
            float,
        ]
        | None
    ):
        comparisons = (
            ("grounded_vs_unsupported", "caption", first.grounding_score, second.grounding_score),
            ("strong_pair_vs_weak_pair", "pairing", first.pairing_score, second.pairing_score),
            ("specific_vs_generic", "caption", first.style_score, second.style_score),
            ("novel_vs_near_duplicate", "caption", first.novelty_score, second.novelty_score),
            ("fresh_vs_recent_overuse", "caption", first.rotation_score, second.rotation_score),
        )
        for category, target, first_value, second_value in comparisons:
            delta = first_value - second_value
            threshold = 0.12 if category == "grounded_vs_unsupported" else 0.18
            if abs(delta) < threshold:
                continue
            preferred, dispreferred = (first, second) if delta > 0 else (second, first)
            return (
                preferred,
                dispreferred,
                target,
                category,
                max(0.25, min(0.8, abs(delta))),
            )
        if (
            first.editorial_angle != second.editorial_angle
            and abs(first.final_score - second.final_score) <= 0.05
        ):
            preferred, dispreferred = (
                (first, second) if first.final_score >= second.final_score else (second, first)
            )
            return preferred, dispreferred, "caption", "same_scene_different_angle", 0.25
        return None

    @staticmethod
    def _key(
        channel_id: int,
        slate_id: int,
        preferred_id: int,
        dispreferred_id: int,
        target: str,
        category: str,
    ) -> str:
        payload = (
            f"hard-negative-v1:{channel_id}:{slate_id}:{preferred_id}:"
            f"{dispreferred_id}:{target}:{category}"
        )
        return f"hard-negative:{hashlib.sha256(payload.encode()).hexdigest()}"

    @staticmethod
    def _number(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("hard-negative strength is not numeric")
        return float(value)

    @staticmethod
    def _policy_version(editorial_brief_json: str) -> str | None:
        try:
            value = json.loads(editorial_brief_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        policy_version = str(value.get("policy_version") or "").strip()
        return policy_version or None
