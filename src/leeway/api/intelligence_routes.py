from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from leeway.analysis.service import AnalysisService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.intelligence.profile import StyleProfileService


class AnnotationCorrectionRequest(BaseModel):
    fields: dict[str, object] = Field(min_length=1)


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
            return AnalysisService(database, settings).correct_annotation(post_id, payload.fields)
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

    @router.get("/profiles/{profile_id}")
    def profile_detail(profile_id: int) -> dict[str, object]:
        try:
            return StyleProfileService(database, settings).detail(profile_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
