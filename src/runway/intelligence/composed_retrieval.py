from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    ComposedRetrievalExample,
    GeneratedAssetLineage,
    MediaAsset,
    Post,
    PostMedia,
    SearchRun,
)
from runway.db.repositories import audit, get_channel
from runway.intelligence.embeddings import ActiveRepresentationResolver, cosine


class ComposedRetrievalPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_asset_id: int
    score: float = Field(ge=-1, le=1)


class LearnedComposedRetrievalProvider(Protocol):
    name: str
    model: str
    version: str
    licensing_verified: bool

    def rank(
        self,
        *,
        reference_image: Path,
        modification_instruction: str,
        candidates: dict[int, Path],
    ) -> list[ComposedRetrievalPrediction]: ...


class ComposedRetrievalService:
    """Evaluation-ready learned composed-retrieval boundary; never implicit activation."""

    version = "learned-composed-retrieval-boundary-v1"
    allowed_label_sources = frozenset({"human", "teacher", "synthetic", "policy"})
    allowed_splits = frozenset({"development", "tuning", "test", "final_holdout"})

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        resolver: ActiveRepresentationResolver | None = None,
    ):
        self.database = database
        self.settings = settings or database.settings
        self.representations = resolver or ActiveRepresentationResolver(database)

    def add_example(
        self,
        *,
        reference_media_asset_id: int,
        modification_instruction: str,
        target_media_asset_id: int,
        label_source: str,
        split: str = "development",
        reviewed: bool = False,
        instruction_source: dict[str, object] | None = None,
    ) -> dict[str, object]:
        instruction = " ".join(modification_instruction.split())
        if not instruction:
            raise ValueError("composed-retrieval modification instruction is required")
        if reference_media_asset_id == target_media_asset_id:
            raise ValueError("composed retrieval reference and target must differ")
        if label_source not in self.allowed_label_sources:
            raise ValueError("unsupported composed-retrieval label source")
        if split not in self.allowed_splits:
            raise ValueError("unsupported composed-retrieval split")
        if label_source == "human" and not reviewed:
            raise ValueError("human composed-retrieval examples require explicit review")
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            eligible = self._channel_media_ids(session, channel_id)
            required = {reference_media_asset_id, target_media_asset_id}
            if not required.issubset(eligible):
                raise ValueError("composed-retrieval media must belong to the configured channel")
            existing = session.scalar(
                select(ComposedRetrievalExample)
                .where(
                    ComposedRetrievalExample.channel_id == channel_id,
                    ComposedRetrievalExample.reference_media_asset_id == reference_media_asset_id,
                    ComposedRetrievalExample.modification_instruction == instruction,
                    ComposedRetrievalExample.target_media_asset_id == target_media_asset_id,
                )
                .limit(1)
            )
            if existing is not None:
                return {"id": existing.id, "created": False, "status": existing.status}
            representation_sets = self.representations.snapshot(channel_id)
            row = ComposedRetrievalExample(
                channel_id=channel_id,
                reference_media_asset_id=reference_media_asset_id,
                modification_instruction=instruction,
                target_media_asset_id=target_media_asset_id,
                label_source=label_source,
                status="reviewed" if reviewed else "candidate",
                split=split,
                reviewed=reviewed,
                instruction_source_json=json.dumps(
                    instruction_source or {},
                    sort_keys=True,
                ),
                representation_sets_json=json.dumps(
                    representation_sets,
                    sort_keys=True,
                ),
                evaluation_json="{}",
            )
            session.add(row)
            session.flush()
            audit(
                session,
                "composed_retrieval_example_added",
                "composed_retrieval_example",
                row.id,
                {
                    "label_source": label_source,
                    "reviewed": reviewed,
                    "split": split,
                    "human_truth": label_source == "human" and reviewed,
                },
            )
            return {"id": row.id, "created": True, "status": row.status}

    def readiness(self) -> dict[str, object]:
        report = self.dataset_report()
        blockers: list[str] = []
        reviewed_value = report["reviewed_human_examples"]
        if isinstance(reviewed_value, bool) or not isinstance(reviewed_value, int):
            raise RuntimeError("composed-retrieval dataset report is malformed")
        if reviewed_value < 50:
            blockers.append("fewer_than_50_reviewed_creator_examples")
        blockers.append("no_configured_evaluated_learned_provider")
        return {
            "version": self.version,
            "status": "blocked" if blockers else "ready",
            "automatic_model_downloads": False,
            "weighted_vector_fusion_is_learned": False,
            "deterministic_fusion_role": "comparison_baseline_only",
            "dataset": report,
            "activation_blockers": blockers,
        }

    def evaluate(
        self,
        provider: LearnedComposedRetrievalProvider,
        *,
        split: str = "test",
    ) -> dict[str, object]:
        if split not in self.allowed_splits:
            raise ValueError("unsupported composed-retrieval evaluation split")
        if not provider.licensing_verified:
            raise ValueError("learned provider licensing must be verified before evaluation")
        started = time.perf_counter()
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            examples = session.scalars(
                select(ComposedRetrievalExample)
                .where(
                    ComposedRetrievalExample.channel_id == channel_id,
                    ComposedRetrievalExample.split == split,
                    ComposedRetrievalExample.reviewed.is_(True),
                )
                .order_by(ComposedRetrievalExample.id)
            ).all()
            media_ids = {
                value
                for row in examples
                for value in (row.reference_media_asset_id, row.target_media_asset_id)
            }
            media = {
                row.id: row
                for row in session.scalars(
                    select(MediaAsset).where(MediaAsset.id.in_(media_ids))
                ).all()
            }
        if not examples:
            raise ValueError("no reviewed composed-retrieval examples exist in this split")
        candidate_paths = {
            media_id: self._media_path(asset)
            for media_id, asset in media.items()
            if media_id in {row.target_media_asset_id for row in examples}
        }
        learned_ranks: list[int] = []
        baseline_ranks: list[int] = []
        per_example: dict[int, dict[str, object]] = {}
        for example in examples:
            predictions = provider.rank(
                reference_image=self._media_path(media[example.reference_media_asset_id]),
                modification_instruction=example.modification_instruction,
                candidates=candidate_paths,
            )
            learned_order = [prediction.media_asset_id for prediction in predictions]
            if set(learned_order) != set(candidate_paths) or len(learned_order) != len(
                candidate_paths
            ):
                raise ValueError("learned composed provider returned an incomplete ranking")
            learned_rank = learned_order.index(example.target_media_asset_id) + 1
            baseline_order, baseline_provenance = self._baseline_order(
                channel_id=channel_id,
                reference=media[example.reference_media_asset_id],
                instruction=example.modification_instruction,
                candidates={media_id: media[media_id] for media_id in candidate_paths},
            )
            baseline_rank = baseline_order.index(example.target_media_asset_id) + 1
            learned_ranks.append(learned_rank)
            baseline_ranks.append(baseline_rank)
            per_example[example.id] = {
                "learned_rank": learned_rank,
                "weighted_fusion_baseline_rank": baseline_rank,
                "baseline_representation": baseline_provenance,
            }
        learned_metrics = self._ranking_metrics(learned_ranks)
        baseline_metrics = self._ranking_metrics(baseline_ranks)
        human_count = sum(row.label_source == "human" for row in examples)
        blockers: list[str] = []
        if human_count < 50:
            blockers.append("fewer_than_50_reviewed_creator_examples")
        if learned_metrics["mrr"] <= baseline_metrics["mrr"]:
            blockers.append("no_mrr_improvement_over_weighted_fusion")
        blockers.extend(
            [
                "concept_preservation_human_gate_not_recorded",
                "modification_compliance_human_gate_not_recorded",
                "duplicate_risk_gate_not_recorded",
                "creator_preference_gate_not_recorded",
            ]
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        with self.database.session() as session:
            for example_id, payload in per_example.items():
                row = session.get(ComposedRetrievalExample, example_id)
                if row is not None:
                    row.evaluation_json = json.dumps(
                        {
                            "version": self.version,
                            "provider": provider.name,
                            "model": provider.model,
                            "model_version": provider.version,
                            **payload,
                        },
                        sort_keys=True,
                    )
            audit(
                session,
                "composed_retrieval_evaluated",
                "channel",
                channel_id,
                {
                    "provider": provider.name,
                    "model": provider.model,
                    "split": split,
                    "example_count": len(examples),
                    "activation_eligible": not blockers,
                    "activation_blockers": blockers,
                },
            )
        return {
            "version": self.version,
            "provider": provider.name,
            "model": provider.model,
            "model_version": provider.version,
            "split": split,
            "example_count": len(examples),
            "reviewed_human_examples": human_count,
            "learned": learned_metrics,
            "weighted_fusion_baseline": baseline_metrics,
            "latency_ms": latency_ms,
            "activation_eligible": not blockers,
            "activation_blockers": blockers,
            "activated": False,
        }

    def dataset_report(self) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            rows = session.scalars(
                select(ComposedRetrievalExample).where(
                    ComposedRetrievalExample.channel_id == channel_id
                )
            ).all()
        sources = Counter(row.label_source for row in rows)
        splits = Counter(row.split for row in rows)
        return {
            "example_count": len(rows),
            "reviewed_count": sum(row.reviewed for row in rows),
            "reviewed_human_examples": sum(
                row.reviewed and row.label_source == "human" for row in rows
            ),
            "label_sources": dict(sorted(sources.items())),
            "splits": dict(sorted(splits.items())),
        }

    def _baseline_order(
        self,
        *,
        channel_id: int,
        reference: MediaAsset,
        instruction: str,
        candidates: dict[int, MediaAsset],
    ) -> tuple[list[int], dict[str, object]]:
        query, resolution = self.representations.multimodal_vector(
            channel_id,
            self._media_path(reference),
            instruction,
            score_purpose="composed_retrieval_weighted_fusion_baseline",
        )
        scored: list[tuple[float, int]] = []
        for media_id, media in candidates.items():
            vector, target_resolution = self.representations.multimodal_vector(
                channel_id,
                self._media_path(media),
                "",
                score_purpose="composed_retrieval_target",
            )
            if target_resolution.representation_set_id != resolution.representation_set_id:
                raise RuntimeError("composed retrieval mixed incompatible representation sets")
            scored.append((cosine(query, vector), media_id))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [media_id for _score, media_id in scored], resolution.as_dict()

    @staticmethod
    def _ranking_metrics(ranks: list[int]) -> dict[str, float]:
        return {
            "mrr": round(sum(1 / rank for rank in ranks) / len(ranks), 6),
            "recall_at_1": round(sum(rank <= 1 for rank in ranks) / len(ranks), 6),
            "recall_at_5": round(sum(rank <= 5 for rank in ranks) / len(ranks), 6),
        }

    @staticmethod
    def _channel_media_ids(session: object, channel_id: int) -> set[int]:
        # SQLAlchemy Session is intentionally duck-typed here to keep this
        # boundary usable by transaction fakes in offline tests.
        scalar_session = session
        historical = set(
            scalar_session.scalars(  # type: ignore[attr-defined]
                select(PostMedia.media_asset_id)
                .join(Post, Post.id == PostMedia.post_id)
                .where(Post.channel_id == channel_id)
            ).all()
        )
        candidates = set(
            scalar_session.scalars(  # type: ignore[attr-defined]
                select(CandidateImage.media_asset_id)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel_id)
            ).all()
        )
        generated = set(
            scalar_session.scalars(  # type: ignore[attr-defined]
                select(GeneratedAssetLineage.media_asset_id).where(
                    GeneratedAssetLineage.channel_id == channel_id
                )
            ).all()
        )
        return historical | candidates | generated

    def _media_path(self, media: MediaAsset) -> Path:
        raw = Path(media.local_path)
        resolved = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"media asset {media.id} escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved
