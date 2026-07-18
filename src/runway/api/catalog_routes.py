from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from runway.capture.schemas import BrowserDomSnapshot
from runway.capture.service import CaptureService
from runway.catalog.service import CatalogService
from runway.config import Settings
from runway.db.base import Database


class FixtureCaptureRequest(BaseModel):
    resume: bool = True
    max_posts: int | None = Field(default=None, ge=1)
    dry_run: bool = False


class EligibilityRequest(BaseModel):
    is_training_eligible: bool


class BrowserCheckpointRequest(BaseModel):
    channel_url: str
    records: list[BrowserDomSnapshot] = Field(min_length=1, max_length=10)
    surface_card_count: int = Field(ge=1)
    surface_tail_key: str = Field(min_length=1, max_length=200)
    finalize: bool = False
    expected_total: int | None = Field(default=None, ge=1)


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

    @router.post("/capture/browser-checkpoint")
    def ingest_browser_checkpoint(
        payload: BrowserCheckpointRequest,
        x_runway_capture_source: str = Header(),
    ) -> dict[str, object]:
        if x_runway_capture_source != "browser-agent":
            raise HTTPException(status_code=400, detail="invalid browser capture source")
        encoded_size = sum(len(record.html.encode("utf-8")) for record in payload.records)
        if encoded_size > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="browser checkpoint exceeds 4 MiB")
        try:
            return capture.append_dom_checkpoint(
                payload.records,
                channel_url=payload.channel_url,
                surface_card_count=payload.surface_card_count,
                surface_tail_key=payload.surface_tail_key,
                finalize=payload.finalize,
                expected_total=payload.expected_total,
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
