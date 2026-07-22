from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import desc, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CandidateExposure, CandidateImage, MediaAsset, SearchRun
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import ActiveRepresentationResolver, cosine
from runway.ranking.diversity import (
    DiversityFingerprint,
    fingerprint_from_candidate,
    fingerprint_similarity,
)


@dataclass(frozen=True)
class CandidateSlateResult:
    candidates: tuple[CandidateImage, ...]
    diagnostics: dict[str, object]


class CandidateSlateOptimizer:
    """Whole-slate active-representation optimizer with deterministic guardrails."""

    version = "active-representation-slate-v2"
    neural_cluster_threshold = 0.82
    semantic_suppression_threshold = 0.93
    deterministic_suppression_threshold = 0.88

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        resolver: ActiveRepresentationResolver | None = None,
    ):
        self.database = database
        self.settings = settings or database.settings
        self.representations = resolver or ActiveRepresentationResolver(database)

    def select(
        self,
        candidates: Sequence[CandidateImage],
        *,
        anchors: Sequence[CandidateImage] = (),
        limit: int | None = None,
        session_key: str,
    ) -> CandidateSlateResult:
        if not session_key.strip():
            raise ValueError("slate optimization session key is required")
        unique = {candidate.id: candidate for candidate in candidates}
        ordered_pool = sorted(
            unique.values(),
            key=lambda row: (-row.final_rank_score, row.id),
        )
        if not ordered_pool:
            return CandidateSlateResult(
                candidates=(),
                diagnostics=self._empty_diagnostics(session_key),
            )
        maximum = len(ordered_pool) if limit is None else max(1, min(limit, len(ordered_pool)))
        channel_id, media = self._validated_media([*ordered_pool, *anchors])
        vectors: dict[int, np.ndarray] = {}
        vector_failures: dict[str, str] = {}
        resolution: dict[str, object] = {}
        for candidate in [*ordered_pool, *anchors]:
            if candidate.id in vectors:
                continue
            asset = media[candidate.media_asset_id]
            try:
                vector, representation = self.representations.image_vector(
                    channel_id,
                    self._media_path(asset),
                    score_purpose="candidate_slate_semantic_diversity",
                )
            except (FileNotFoundError, ValueError) as exc:
                vector_failures[str(candidate.id)] = f"{type(exc).__name__}: {exc}"
                continue
            vectors[candidate.id] = vector
            resolution = representation.as_dict()
        eligible = [candidate for candidate in ordered_pool if candidate.id in vectors]
        neural_guardrail_active = bool(resolution and resolution.get("provider") != "runway-local")
        neural_clusters = self._semantic_clusters(eligible, vectors)
        recent_fatigue = self._recent_fatigue(channel_id)
        fingerprints = {
            candidate.id: fingerprint_from_candidate(candidate)
            for candidate in [*eligible, *anchors]
        }
        selected: list[CandidateImage] = []
        selected_utility: list[float] = []
        remaining = list(eligible)
        candidate_diagnostics: dict[str, dict[str, object]] = {
            str(candidate.id): {
                "pre_display_score": candidate.final_rank_score,
                "neural_cluster": neural_clusters.get(candidate.id),
                "representation_available": candidate.id in vectors,
                "suppression_reasons": [],
            }
            for candidate in ordered_pool
        }
        for candidate_id, error in vector_failures.items():
            candidate_diagnostics[candidate_id]["suppression_reasons"] = [
                "active_representation_unavailable"
            ]
            candidate_diagnostics[candidate_id]["representation_error"] = error

        while remaining and len(selected) < maximum:
            evaluations = [
                self._evaluate(
                    candidate,
                    selected=selected,
                    anchors=anchors,
                    vectors=vectors,
                    fingerprints=fingerprints,
                    neural_clusters=neural_clusters,
                    recent_fatigue=recent_fatigue,
                    enforce_semantic_suppression=neural_guardrail_active,
                )
                for candidate in remaining
            ]
            eligible_evaluations = [row for row in evaluations if not row[1]]
            if not eligible_evaluations:
                for candidate, reasons, penalties, utility in evaluations:
                    candidate_diagnostics[str(candidate.id)].update(
                        {
                            "suppression_reasons": reasons,
                            "diversity_penalties": penalties,
                            "slate_utility": round(utility, 6),
                        }
                    )
                break
            candidate, _reasons, penalties, utility = max(
                eligible_evaluations,
                key=lambda row: (row[3], row[0].final_rank_score, -row[0].id),
            )
            selected.append(candidate)
            selected_utility.append(utility)
            candidate_diagnostics[str(candidate.id)].update(
                {
                    "selection_reason": "highest safe whole-slate utility",
                    "suppression_reasons": [],
                    "diversity_penalties": penalties,
                    "slate_utility": round(utility, 6),
                    "display_position": len(selected),
                }
            )
            remaining.remove(candidate)

        selected_ids = {candidate.id for candidate in selected}
        for candidate in remaining:
            if candidate.id in selected_ids:
                continue
            reasons, penalties, utility = self._evaluate(
                candidate,
                selected=selected,
                anchors=anchors,
                vectors=vectors,
                fingerprints=fingerprints,
                neural_clusters=neural_clusters,
                recent_fatigue=recent_fatigue,
                enforce_semantic_suppression=neural_guardrail_active,
            )[1:]
            candidate_diagnostics[str(candidate.id)].update(
                {
                    "suppression_reasons": reasons or ["outside_slate_limit"],
                    "diversity_penalties": penalties,
                    "slate_utility": round(utility, 6),
                }
            )

        cluster_counts = Counter(neural_clusters.get(row.id, "unclustered") for row in selected)
        fingerprints_selected = [fingerprints[row.id] for row in selected]
        coverage = self._coverage(fingerprints_selected, selected, neural_clusters)
        diagnostics: dict[str, object] = {
            "version": self.version,
            "session_key": session_key,
            "status": "completed" if selected else "abstained_no_safe_semantic_slate",
            "candidate_count": len(ordered_pool),
            "represented_candidate_count": len(eligible),
            "selected_ids": [row.id for row in selected],
            "candidate_diagnostics": candidate_diagnostics,
            "final_slate_utility": round(sum(selected_utility), 6),
            "cluster_coverage": coverage,
            "largest_cluster_share": (
                max(cluster_counts.values(), default=0) / len(selected) if selected else None
            ),
            "representation": resolution,
            "neural_active": neural_guardrail_active,
            "deterministic_guardrails": {
                "concept_key_repeat": "hard_suppression",
                "fingerprint_similarity_threshold": self.deterministic_suppression_threshold,
                "semantic_similarity_threshold": (
                    self.semantic_suppression_threshold if neural_guardrail_active else None
                ),
                "deterministic_image_similarity": (
                    "soft_penalty_only" if not neural_guardrail_active else "not_applicable"
                ),
            },
            "vector_failures": vector_failures,
            "randomized": False,
            "exploration_policy": "deterministic_slate_v2",
        }
        return CandidateSlateResult(candidates=tuple(selected), diagnostics=diagnostics)

    def _evaluate(
        self,
        candidate: CandidateImage,
        *,
        selected: Sequence[CandidateImage],
        anchors: Sequence[CandidateImage],
        vectors: dict[int, np.ndarray],
        fingerprints: dict[int, DiversityFingerprint],
        neural_clusters: dict[int, str],
        recent_fatigue: Counter[str],
        enforce_semantic_suppression: bool,
    ) -> tuple[CandidateImage, list[str], dict[str, float], float]:
        references = [*anchors, *selected]
        fingerprint = fingerprints[candidate.id]
        deterministic_similarity = max(
            (
                fingerprint_similarity(fingerprint, fingerprints[other.id])
                for other in references
                if other.id in fingerprints
            ),
            default=0.0,
        )
        semantic_similarity = max(
            (
                max(0.0, cosine(vectors[candidate.id], vectors[other.id]))
                for other in references
                if other.id in vectors
            ),
            default=0.0,
        )
        same_concept = any(
            fingerprint.concept_key == fingerprints[other.id].concept_key
            for other in references
            if other.id in fingerprints
        )
        reasons: list[str] = []
        if same_concept:
            reasons.append("deterministic_concept_repeat")
        if deterministic_similarity >= self.deterministic_suppression_threshold:
            reasons.append("deterministic_near_repeat")
        if (
            enforce_semantic_suppression
            and semantic_similarity >= self.semantic_suppression_threshold
        ):
            reasons.append("active_representation_near_repeat")
        fatigue_count = recent_fatigue[candidate.diversity_cluster_key or "unclustered"]
        source_repeat = sum(
            (other.source_domain or "unknown") == (candidate.source_domain or "unknown")
            for other in selected[-10:]
        )
        budget_penalty = self._rolling_budget_penalty(candidate, selected, fingerprint)
        penalties = {
            "semantic_similarity": round(0.18 * semantic_similarity, 6),
            "deterministic_similarity": round(0.12 * deterministic_similarity, 6),
            "session_fatigue": round(min(0.24, 0.025 * fatigue_count), 6),
            "source_repetition": round(min(0.16, 0.04 * source_repeat), 6),
            "rolling_budget": round(budget_penalty, 6),
        }
        utility = (
            0.72 * candidate.final_rank_score
            + 0.10 * candidate.novelty_score
            + 0.06 * candidate.quality_score
            - sum(penalties.values())
        )
        return candidate, reasons, penalties, utility

    @staticmethod
    def _rolling_budget_penalty(
        candidate: CandidateImage,
        selected: Sequence[CandidateImage],
        fingerprint: DiversityFingerprint,
    ) -> float:
        recent = selected[-10:]
        if not recent:
            return 0.0
        recent_fingerprints = [fingerprint_from_candidate(row) for row in recent]
        dimensions = (
            (fingerprint.franchise, [row.franchise for row in recent_fingerprints], 0.03),
            (fingerprint.scene_family, [row.scene_family for row in recent_fingerprints], 0.05),
            (fingerprint.setting_family, [row.setting_family for row in recent_fingerprints], 0.03),
            (fingerprint.emotion_family, [row.emotion_family for row in recent_fingerprints], 0.04),
            (
                fingerprint.composition_family,
                [row.composition_family for row in recent_fingerprints],
                0.04,
            ),
        )
        penalty = sum(weight * values.count(value) for value, values, weight in dimensions)
        entity_counts = Counter(entity for row in recent_fingerprints for entity in row.characters)
        penalty += min(
            0.12, 0.025 * sum(entity_counts[entity] for entity in fingerprint.characters)
        )
        return min(0.3, penalty)

    def _validated_media(
        self,
        candidates: Sequence[CandidateImage],
    ) -> tuple[int, dict[int, MediaAsset]]:
        candidate_ids = {row.id for row in candidates}
        media_ids = {row.media_asset_id for row in candidates}
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            valid_candidate_ids = set(
                session.scalars(
                    select(CandidateImage.id)
                    .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                    .where(
                        SearchRun.channel_id == channel_id,
                        CandidateImage.id.in_(candidate_ids),
                    )
                ).all()
            )
            media = {
                row.id: row
                for row in session.scalars(
                    select(MediaAsset).where(MediaAsset.id.in_(media_ids))
                ).all()
            }
        if valid_candidate_ids != candidate_ids:
            raise ValueError("candidate slate contains missing or cross-channel candidates")
        if set(media) != media_ids:
            raise ValueError("candidate slate contains missing media assets")
        return channel_id, media

    def _recent_fatigue(self, channel_id: int) -> Counter[str]:
        with self.database.session() as session:
            rows = session.scalars(
                select(CandidateExposure)
                .where(
                    CandidateExposure.channel_id == channel_id,
                    CandidateExposure.event_type.in_(
                        ("shown", "skipped", "replaced", "rejected", "fewer_like_this")
                    ),
                )
                .order_by(desc(CandidateExposure.created_at), desc(CandidateExposure.id))
                .limit(500)
            ).all()
        return Counter(row.cluster_key or "unclustered" for row in rows)

    def _media_path(self, media: MediaAsset) -> Path:
        raw = Path(media.local_path)
        resolved = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"media asset {media.id} escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    def _semantic_clusters(
        self,
        candidates: Sequence[CandidateImage],
        vectors: dict[int, np.ndarray],
    ) -> dict[int, str]:
        representatives: list[int] = []
        assignments: dict[int, str] = {}
        for candidate in sorted(candidates, key=lambda row: (-row.final_rank_score, row.id)):
            nearest = max(
                (
                    (cosine(vectors[candidate.id], vectors[representative]), representative)
                    for representative in representatives
                ),
                default=(-1.0, -1),
            )
            if nearest[0] < self.neural_cluster_threshold:
                representatives.append(candidate.id)
                representative_id = candidate.id
            else:
                representative_id = nearest[1]
            assignments[candidate.id] = f"semantic-{representative_id}"
        return assignments

    @staticmethod
    def _coverage(
        fingerprints: Sequence[DiversityFingerprint],
        selected: Sequence[CandidateImage],
        clusters: dict[int, str],
    ) -> dict[str, int]:
        return {
            "topic_or_franchise": len({row.franchise for row in fingerprints}),
            "entity_or_character": len({value for row in fingerprints for value in row.characters}),
            "scene": len({row.scene_family for row in fingerprints}),
            "setting": len({row.setting_family for row in fingerprints}),
            "emotion": len({row.emotion_family for row in fingerprints}),
            "composition": len({row.composition_family for row in fingerprints}),
            "source": len({row.source_domain or "unknown" for row in selected}),
            "visual_cluster": len({clusters.get(row.id, "unclustered") for row in selected}),
        }

    def _empty_diagnostics(self, session_key: str) -> dict[str, object]:
        return {
            "version": self.version,
            "session_key": session_key,
            "status": "empty_pool",
            "candidate_count": 0,
            "represented_candidate_count": 0,
            "selected_ids": [],
            "candidate_diagnostics": {},
            "final_slate_utility": 0.0,
            "cluster_coverage": {},
            "largest_cluster_share": None,
            "representation": {},
            "neural_active": False,
            "randomized": False,
            "exploration_policy": "deterministic_slate_v2",
        }
