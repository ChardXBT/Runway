from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from leeway.audit.service import AuditService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.discovery.service import DiscoveryService
from leeway.proposals.service import ProposalService
from leeway.publishing.internal import InternalPublisher
from leeway.services.settings import SettingsService


class GenerationRequest(BaseModel):
    days: int = Field(default=10, ge=1, le=30)
    start_date: date | None = None


class CaptionEditRequest(BaseModel):
    final_caption: str = Field(min_length=1, max_length=1000)


class AlternativeRequest(BaseModel):
    index: int = Field(ge=0)


class RejectionRequest(BaseModel):
    reason: str = Field(default="not a fit", min_length=1, max_length=500)


class ReplacementRequest(BaseModel):
    candidate_id: int | None = None


class RescheduleRequest(BaseModel):
    new_date: date


class MetadataCorrectionRequest(BaseModel):
    fields: dict[str, object] = Field(min_length=1)


class SettingsUpdateRequest(BaseModel):
    fields: dict[str, Any] = Field(min_length=1)


def build_proposal_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["proposals"])
    proposals = ProposalService(database, settings)

    @router.post("/generation-runs")
    def create_generation(payload: GenerationRequest) -> dict[str, object]:
        try:
            return asyncio.run(
                proposals.generate_batch(days=payload.days, start_date=payload.start_date)
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/generation-runs/{run_id}")
    def generation_status(run_id: int) -> dict[str, object]:
        try:
            return proposals.generation_status(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/proposals")
    def proposal_list(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, object]]:
        return proposals.list_proposals(status=status, limit=limit)

    @router.get("/proposals/{proposal_id}")
    def proposal_detail(proposal_id: int) -> dict[str, object]:
        try:
            return proposals.detail(proposal_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/proposals/{proposal_id}")
    def edit_caption(proposal_id: int, payload: CaptionEditRequest) -> dict[str, object]:
        try:
            return proposals.edit_caption(proposal_id, payload.final_caption)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/select-alternative")
    def select_alternative(proposal_id: int, payload: AlternativeRequest) -> dict[str, object]:
        try:
            return proposals.select_alternative(proposal_id, payload.index)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: int) -> dict[str, object]:
        try:
            return proposals.approve(proposal_id)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: int, payload: RejectionRequest) -> dict[str, object]:
        try:
            return proposals.reject(proposal_id, payload.reason)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/regenerate")
    def regenerate(proposal_id: int) -> dict[str, object]:
        try:
            return asyncio.run(proposals.regenerate_captions(proposal_id))
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/replace")
    def replace(proposal_id: int, payload: ReplacementRequest) -> dict[str, object]:
        try:
            return asyncio.run(proposals.replace_image(proposal_id, payload.candidate_id))
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/reschedule")
    def reschedule(proposal_id: int, payload: RescheduleRequest) -> dict[str, object]:
        try:
            return proposals.reschedule(proposal_id, payload.new_date)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/metadata")
    def correct_metadata(proposal_id: int, payload: MetadataCorrectionRequest) -> dict[str, object]:
        try:
            return proposals.correct_candidate_metadata(proposal_id, payload.fields)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/block-image")
    def block_image(proposal_id: int) -> dict[str, object]:
        try:
            detail = proposals.detail(proposal_id)
            DiscoveryService(database, settings).reject_candidate(
                cast(int, detail["candidate_image_id"]), "user_blocked"
            )
            return asyncio.run(proposals.replace_image(proposal_id))
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/block-domain")
    def block_domain(proposal_id: int) -> dict[str, object]:
        try:
            detail = proposals.detail(proposal_id)
            DiscoveryService(database, settings).block_domain(
                cast(int, detail["candidate_image_id"])
            )
            return asyncio.run(proposals.replace_image(proposal_id))
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/internal-schedule")
    def internal_schedule(proposal_id: int) -> dict[str, object]:
        try:
            asyncio.run(InternalPublisher(database, settings).schedule_post(proposal_id))
            return proposals.detail(proposal_id)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/queue")
    def queue(days: int = Query(default=10, ge=1, le=30)) -> dict[str, object]:
        return proposals.queue_status(days=days)

    @router.get("/activity")
    def activity(
        event_type: str | None = None,
        entity_type: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, object]]:
        return AuditService(database).list_events(
            event_type=event_type, entity_type=entity_type, limit=limit
        )

    @router.get("/settings/full")
    def settings_view() -> dict[str, object]:
        return SettingsService(database, settings).get()

    @router.patch("/settings/full")
    def settings_update(payload: SettingsUpdateRequest) -> dict[str, object]:
        try:
            return SettingsService(database, settings).update(payload.fields)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
