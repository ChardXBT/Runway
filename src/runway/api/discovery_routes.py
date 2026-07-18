from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from runway.captions.service import CaptionService
from runway.config import Settings
from runway.db.base import Database
from runway.discovery.service import DiscoveryService


class SearchRunRequest(BaseModel):
    days: int = Field(default=10, ge=1, le=30)
    provider: str = "fixture"
    manual_urls: list[str] = Field(default_factory=list)
    dry_run: bool = False
    live: bool = False


class CandidateRejectRequest(BaseModel):
    reason: str = "user_rejected"


def build_discovery_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["discovery"])

    @router.post("/search-runs")
    def create_search(payload: SearchRunRequest) -> dict[str, object]:
        try:
            return asyncio.run(
                DiscoveryService(database, settings).discover(
                    days=payload.days,
                    provider_name=payload.provider,
                    manual_urls=payload.manual_urls,
                    dry_run=payload.dry_run,
                    live=payload.live,
                )
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/search-runs/{run_id}")
    def search_status(run_id: int) -> dict[str, object]:
        try:
            return DiscoveryService(database, settings).run_status(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/search-runs/{run_id}/results")
    def search_results(run_id: int) -> list[dict[str, object]]:
        return DiscoveryService(database, settings).list_candidates(run_id=run_id)

    @router.get("/candidates/{candidate_id}")
    def candidate_detail(candidate_id: int) -> dict[str, object]:
        try:
            return DiscoveryService(database, settings).detail(candidate_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/captions")
    def candidate_captions(candidate_id: int) -> dict[str, object]:
        try:
            return asyncio.run(
                CaptionService(database, settings).generate(candidate_id)
            ).model_dump()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/reject")
    def reject_candidate(candidate_id: int, payload: CandidateRejectRequest) -> dict[str, object]:
        try:
            return DiscoveryService(database, settings).reject_candidate(
                candidate_id, payload.reason
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/candidates/{candidate_id}/block-domain")
    def block_domain(candidate_id: int) -> dict[str, object]:
        try:
            return DiscoveryService(database, settings).block_domain(candidate_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
