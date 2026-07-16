from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from leeway.capture.service import CaptureService
from leeway.catalog.service import CatalogService
from leeway.config import Settings
from leeway.db.base import Database


class FixtureCaptureRequest(BaseModel):
    resume: bool = True
    max_posts: int | None = Field(default=None, ge=1)
    dry_run: bool = False


class EligibilityRequest(BaseModel):
    is_training_eligible: bool


def build_catalog_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["catalogue"])
    capture = CaptureService(database, settings)
    catalog = CatalogService(database, settings)

    @router.get("/capture/runs")
    def capture_runs() -> dict[str, object]:
        return {"latest": capture.latest_status()}

    @router.post("/capture/fixture")
    def run_fixture(payload: FixtureCaptureRequest) -> dict[str, object]:
        return capture.run_fixture(
            resume=payload.resume,
            max_posts=payload.max_posts,
            dry_run=payload.dry_run,
        ).model_dump()

    @router.get("/catalog/status")
    def catalog_status() -> dict[str, object]:
        return catalog.status()

    @router.get("/catalog/verify")
    def catalog_verify() -> dict[str, Any]:
        return catalog.verify(write_reports=True)

    @router.get("/catalog")
    def catalog_list(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str | None = None,
        post_type: str | None = None,
        training_eligible: bool | None = None,
        franchise: str | None = None,
        character: str | None = None,
    ) -> list[dict[str, object]]:
        return catalog.list_posts(
            limit=limit,
            offset=offset,
            search=search,
            post_type=post_type,
            training_eligible=training_eligible,
            franchise=franchise,
            character=character,
        )

    @router.patch("/catalog/{post_id}/eligibility")
    def update_eligibility(post_id: int, payload: EligibilityRequest) -> dict[str, object]:
        try:
            return catalog.set_training_eligibility(post_id, payload.is_training_eligible)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/catalog/{post_id}")
    def catalog_detail(post_id: int) -> dict[str, object]:
        try:
            return catalog.detail(post_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
