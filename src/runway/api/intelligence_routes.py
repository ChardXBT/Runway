from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from runway.analysis.service import AnalysisService
from runway.captions.preference_models import MINIMUM_LABELS, PREFERENCE_TARGETS
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    BlindStudy,
    FeedbackSignal,
    PairwisePreference,
    PreferenceDataset,
    PreferenceModelVersion,
)
from runway.db.repositories import get_channel
from runway.intelligence.profile import StyleProfileService


class AnnotationCorrectionRequest(BaseModel):
    fields: dict[str, object] = Field(min_length=1)
    review_note: str | None = Field(default=None, max_length=1000)


def build_intelligence_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["intelligence"])

    @router.post("/analysis/history")
    def analyze_history(resume: bool = True) -> dict[str, int]:
        return asyncio.run(AnalysisService(database, settings).analyze_history(resume=resume))

    @router.get("/annotations/{post_id}")
    def annotation(post_id: int) -> dict[str, object]:
        try:
            return AnalysisService(database, settings).effective_annotation(post_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/annotations/{post_id}")
    def correct_annotation(post_id: int, payload: AnnotationCorrectionRequest) -> dict[str, object]:
        try:
            return AnalysisService(database, settings).review_annotation(
                post_id,
                payload.fields,
                review_note=payload.review_note,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/catalog/{post_id}/similar")
    def similar_posts(post_id: int) -> list[dict[str, object]]:
        return AnalysisService(database, settings).similar_posts(post_id)

    @router.get("/profiles")
    def profiles() -> list[dict[str, object]]:
        return StyleProfileService(database, settings).list_profiles()

    @router.get("/profiles/active")
    def active_profile() -> dict[str, object]:
        try:
            return StyleProfileService(database, settings).active()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/profiles/build")
    def build_profile() -> dict[str, object]:
        try:
            return asyncio.run(StyleProfileService(database, settings).build())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/profiles/evaluate")
    def evaluate_profile() -> dict[str, object]:
        try:
            return StyleProfileService(database, settings).evaluate()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/profiles/evaluation")
    def profile_evaluation() -> dict[str, object]:
        try:
            return StyleProfileService(database, settings).latest_evaluation()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/intelligence/status")
    def intelligence_status() -> dict[str, object]:
        with database.session() as session:
            channel_id = get_channel(session, settings.channel_handle).id
            signal_counts = {
                str(target): int(count)
                for target, count in session.execute(
                    select(FeedbackSignal.target, func.count(FeedbackSignal.id))
                    .where(FeedbackSignal.channel_id == channel_id)
                    .group_by(FeedbackSignal.target)
                )
            }
            human_pairwise_counts = {
                str(target): int(count)
                for target, count in session.execute(
                    select(
                        PairwisePreference.target,
                        func.count(PairwisePreference.id),
                    )
                    .where(
                        PairwisePreference.channel_id == channel_id,
                        PairwisePreference.label_source == "human",
                    )
                    .group_by(PairwisePreference.target)
                )
            }
            training_pairwise_counts = {
                str(target): int(count)
                for target, count in session.execute(
                    select(
                        PairwisePreference.target,
                        func.count(PairwisePreference.id),
                    )
                    .where(
                        PairwisePreference.channel_id == channel_id,
                        PairwisePreference.label_source == "human",
                        PairwisePreference.learning_split.in_(("development", "tuning")),
                    )
                    .group_by(PairwisePreference.target)
                )
            }
            dataset_count = int(
                session.scalar(
                    select(func.count(PreferenceDataset.dataset_id)).where(
                        PreferenceDataset.channel_id == channel_id
                    )
                )
                or 0
            )
            active_models = [
                {"id": row.id, "target": row.target, "label_count": row.label_count}
                for row in session.scalars(
                    select(PreferenceModelVersion)
                    .where(
                        PreferenceModelVersion.channel_id == channel_id,
                        PreferenceModelVersion.active.is_(True),
                    )
                    .order_by(PreferenceModelVersion.target)
                ).all()
            ]
            blind_study_responses = int(
                session.scalar(
                    select(func.coalesce(func.sum(BlindStudy.response_count), 0)).where(
                        BlindStudy.channel_id == channel_id
                    )
                )
                or 0
            )
        normalized_signals = {target: signal_counts.get(target, 0) for target in PREFERENCE_TARGETS}
        normalized_human_pairwise = {
            target: human_pairwise_counts.get(target, 0) for target in PREFERENCE_TARGETS
        }
        normalized_training_pairwise = {
            target: training_pairwise_counts.get(target, 0) for target in PREFERENCE_TARGETS
        }
        trainable_targets = [
            target
            for target in PREFERENCE_TARGETS
            if normalized_training_pairwise[target] >= MINIMUM_LABELS[target]
        ]
        active_model_targets = sorted({str(model["target"]) for model in active_models})
        return {
            "feedback_signals": {
                **normalized_signals,
                "total": sum(normalized_signals.values()),
            },
            "human_pairwise_labels": sum(normalized_human_pairwise.values()),
            "human_pairwise_labels_by_target": normalized_human_pairwise,
            "training_pairwise_labels_by_target": normalized_training_pairwise,
            "preference_datasets": dataset_count,
            "active_models": active_models,
            "active_model_targets": active_model_targets,
            "training_minimum_labels_by_target": MINIMUM_LABELS,
            "training_minimum_labels_per_target": min(MINIMUM_LABELS.values()),
            "trainable_targets": trainable_targets,
            "blind_study_responses": blind_study_responses,
            "blind_study_target": 50,
            "state": (
                "active"
                if active_models
                else "ready_to_train"
                if trainable_targets
                else "collecting_creator_labels"
            ),
        }

    @router.get("/profiles/{profile_id}")
    def profile_detail(profile_id: int) -> dict[str, object]:
        try:
            return StyleProfileService(database, settings).detail(profile_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
