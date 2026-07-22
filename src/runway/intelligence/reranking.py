from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateExposure,
    CandidateImage,
    CaptionCandidateRecord,
    CaptionSlate,
    MediaAsset,
    MultimodalRerankRun,
)
from runway.intelligence.embeddings import ActiveRepresentationResolver, configuration_hash, cosine


class JointRerankScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factual_grounding: float = Field(ge=0, le=1)
    caption_quality: float = Field(ge=0, le=1)
    image_quality: float = Field(ge=0, le=1)
    image_caption_compatibility: float = Field(ge=0, le=1)
    creator_preference: float = Field(ge=0, le=1)
    style_fit: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    rotation_fit: float = Field(ge=0, le=1)
    session_fit: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)
    final_score: float = Field(ge=0, le=1)


class JointMultimodalReranker:
    """Auditable joint reranker; current baseline runs in shadow until human-gated."""

    prompt_version = "joint-multimodal-rerank-v1"
    configuration: dict[str, object] = {
        "version": "joint-reranker-shadow-v1",
        "activation": "shadow_only_until_blind_creator_gate",
        "display_limit": 3,
        "weights": {
            "factual_grounding": 0.18,
            "caption_quality": 0.12,
            "image_quality": 0.08,
            "image_caption_compatibility": 0.14,
            "creator_preference": 0.16,
            "style_fit": 0.08,
            "novelty": 0.09,
            "rotation_fit": 0.06,
            "session_fit": 0.09,
            "uncertainty": -0.12,
        },
        "slate_pairwise_similarity_penalty": 0.16,
        "angle_repeat_penalty": 0.08,
    }

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        resolver: ActiveRepresentationResolver | None = None,
    ):
        self.database = database
        self.settings = settings or database.settings
        self.representations = resolver or ActiveRepresentationResolver(database)

    def shadow_evaluate(self, caption_slate_id: int) -> dict[str, object]:
        started = time.perf_counter()
        with self.database.session() as session:
            slate = session.get(CaptionSlate, caption_slate_id)
            if slate is None:
                raise LookupError(f"caption slate {caption_slate_id} was not found")
            candidate = session.get(CandidateImage, slate.candidate_image_id)
            if candidate is None:
                raise LookupError("caption slate candidate image is missing")
            media = session.get(MediaAsset, candidate.media_asset_id)
            if media is None:
                raise LookupError("caption slate media asset is missing")
            rows = session.scalars(
                select(CaptionCandidateRecord)
                .where(
                    CaptionCandidateRecord.caption_slate_id == slate.id,
                    CaptionCandidateRecord.eligible.is_(True),
                )
                .order_by(CaptionCandidateRecord.rank, CaptionCandidateRecord.id)
            ).all()
            channel_id = slate.channel_id
            baseline_order = [row.id for row in rows]
            candidate_id = candidate.id
            media_path = self._media_path(media)
            cluster_key = candidate.diversity_cluster_key
            session_fatigue = int(
                session.scalar(
                    select(func.count(CandidateExposure.id)).where(
                        CandidateExposure.channel_id == channel_id,
                        CandidateExposure.cluster_key == cluster_key,
                        CandidateExposure.event_type.in_(("shown", "skipped", "replaced")),
                    )
                )
                or 0
            )
            input_payload = {
                "candidate_image_id": candidate.id,
                "caption_slate_id": slate.id,
                "editorial_brief": json.loads(slate.editorial_brief_json),
                "retrieval_selected": json.loads(slate.retrieval_selected_evidence_json),
                "model_supplied": json.loads(slate.model_supplied_evidence_json),
                "model_cited": json.loads(slate.model_cited_evidence_json),
                "ranker_used": json.loads(slate.ranker_used_evidence_json),
                "content_mode": self._content_mode(slate.editorial_brief_json),
                "session_fatigue": session_fatigue,
                "candidate_ids": baseline_order,
            }
        if len(rows) < 3:
            raise ValueError("joint reranking requires at least three eligible captions")

        active_pair_set = self.representations.store.active_set(
            channel_id=channel_id,
            scope="historical_multimodal",
            purpose="historical_pair_semantics",
        )
        historical_pairs = (
            self.representations.store.records_for_set(active_pair_set.id)
            if active_pair_set is not None
            else []
        )
        scores: dict[int, JointRerankScores] = {}
        representations: dict[str, object] = self.representations.snapshot(channel_id)
        for row in rows:
            pair_vector, pair_resolution = self.representations.multimodal_vector(
                channel_id,
                media_path,
                row.text,
                score_purpose="joint_rerank_pair",
            )
            similarities = sorted(
                (
                    max(0.0, cosine(pair_vector, self.representations.store.vectors(record)[0]))
                    for record in historical_pairs
                ),
                reverse=True,
            )
            historical_compatibility = (
                sum(similarities[: min(5, len(similarities))]) / min(5, len(similarities))
                if similarities
                else row.pairing_score
            )
            image_quality = max(0.0, min(1.0, candidate.quality_score))
            caption_quality = max(
                0.0,
                min(1.0, (row.style_score + row.policy_score + row.preference_score) / 3),
            )
            session_fit = max(0.0, 1.0 - min(session_fatigue, 10) / 10)
            uncertainty = max(
                0.0,
                min(1.0, 1.0 - min(row.grounding_score, row.generator_confidence)),
            )
            component_values = {
                "factual_grounding": row.grounding_score,
                "caption_quality": caption_quality,
                "image_quality": image_quality,
                "image_caption_compatibility": max(
                    0.0,
                    min(1.0, 0.55 * historical_compatibility + 0.45 * row.pairing_score),
                ),
                "creator_preference": row.preference_score,
                "style_fit": row.style_score,
                "novelty": row.novelty_score,
                "rotation_fit": row.rotation_score,
                "session_fit": session_fit,
                "uncertainty": uncertainty,
            }
            weights = self._weights()
            final = sum(weights[name] * value for name, value in component_values.items())
            scores[row.id] = JointRerankScores(
                **component_values,
                final_score=max(0.0, min(1.0, final)),
            )
            representations[f"caption_candidate:{row.id}:pair"] = pair_resolution.as_dict()

        final_order = self._optimize_slate(channel_id, rows, scores)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        first_resolution = cast_dict(
            representations.get(f"caption_candidate:{rows[0].id}:pair", {})
        )
        with self.database.session() as session:
            run = MultimodalRerankRun(
                channel_id=channel_id,
                candidate_image_id=candidate_id,
                caption_slate_id=caption_slate_id,
                status="shadow_completed",
                provider=str(first_resolution.get("provider", "runway-local")),
                model=str(first_resolution.get("model", "unknown")),
                prompt_version=self.prompt_version,
                label_source="policy",
                input_json=json.dumps(input_payload, sort_keys=True),
                component_scores_json=json.dumps(
                    {key: value.model_dump() for key, value in scores.items()},
                    sort_keys=True,
                ),
                baseline_order_json=json.dumps(baseline_order),
                final_order_json=json.dumps(final_order),
                changed_order=final_order != baseline_order[: len(final_order)],
                configuration_json=json.dumps(self.configuration, sort_keys=True),
                configuration_hash=configuration_hash(self.configuration),
                representation_sets_json=json.dumps(representations, sort_keys=True),
                latency_ms=latency_ms,
            )
            session.add(run)
            session.flush()
            run_id = run.id
            persisted_slate = session.get(CaptionSlate, caption_slate_id)
            if persisted_slate is not None:
                persisted_slate.reranker_run_json = json.dumps(
                    {
                        "run_id": run_id,
                        "status": "shadow_completed",
                        "activated": False,
                        "changed_order": run.changed_order,
                    },
                    sort_keys=True,
                )
            persisted_rows = {
                row.id: row
                for row in session.scalars(
                    select(CaptionCandidateRecord).where(
                        CaptionCandidateRecord.id.in_(list(scores))
                    )
                ).all()
            }
            for row_id, value in scores.items():
                if row_id in persisted_rows:
                    persisted_rows[row_id].reranker_result_json = json.dumps(
                        {
                            "run_id": run_id,
                            "scores": value.model_dump(),
                            "shadow_rank": (
                                final_order.index(row_id) + 1 if row_id in final_order else None
                            ),
                            "selected_for_shadow_slate": row_id in final_order,
                            "activated": False,
                        },
                        sort_keys=True,
                    )
        return {
            "run_id": run_id,
            "status": "shadow_completed",
            "activated": False,
            "activation_blocker": "blind creator reranker gate has no qualifying results",
            "baseline_order": baseline_order,
            "final_order": final_order,
            "changed_order": final_order != baseline_order[: len(final_order)],
            "scores": {key: value.model_dump() for key, value in scores.items()},
            "latency_ms": latency_ms,
        }

    def _optimize_slate(
        self,
        channel_id: int,
        rows: Sequence[CaptionCandidateRecord],
        scores: dict[int, JointRerankScores],
    ) -> list[int]:
        selected: list[CaptionCandidateRecord] = []
        remaining = list(rows)
        while remaining and len(selected) < 3:
            best: CaptionCandidateRecord | None = None
            best_utility = float("-inf")
            for row in remaining:
                similarity_penalty = max(
                    (
                        max(
                            0.0,
                            self.representations.text_similarity(
                                channel_id,
                                row.text,
                                prior.text,
                                score_purpose="slate_caption_diversity",
                            ).score,
                        )
                        for prior in selected
                    ),
                    default=0.0,
                )
                angle_repeat = any(
                    row.editorial_angle == prior.editorial_angle for prior in selected
                )
                utility = (
                    scores[row.id].final_score
                    - self._configuration_float("slate_pairwise_similarity_penalty")
                    * similarity_penalty
                    - self._configuration_float("angle_repeat_penalty") * angle_repeat
                )
                if utility > best_utility or (
                    utility == best_utility and (best is None or row.id < best.id)
                ):
                    best = row
                    best_utility = utility
            assert best is not None
            selected.append(best)
            remaining.remove(best)
        return [row.id for row in selected]

    def _weights(self) -> dict[str, float]:
        value = self.configuration["weights"]
        if not isinstance(value, dict):
            raise RuntimeError("joint reranker weights are malformed")
        return {str(key): float(weight) for key, weight in value.items()}

    def _configuration_float(self, key: str) -> float:
        value = self.configuration[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"joint reranker configuration {key!r} is not numeric")
        return float(value)

    def _media_path(self, media: MediaAsset) -> Path:
        raw = Path(media.local_path)
        resolved = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"media asset {media.id} escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    @staticmethod
    def _content_mode(editorial_brief_json: str) -> object:
        try:
            brief = json.loads(editorial_brief_json)
        except (TypeError, json.JSONDecodeError):
            return None
        return brief.get("content_mode") if isinstance(brief, dict) else None


def cast_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
