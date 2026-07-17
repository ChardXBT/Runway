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
from leeway.editorial.service import EditorialService
from leeway.proposals.service import ProposalService
from leeway.publishing.internal import InternalPublisher
from leeway.publishing.queue import PublisherQueueCoordinator
from leeway.publishing.youtube import YouTubeBrowserPublisher
from leeway.services.settings import SettingsService


class GenerationRequest(BaseModel):
    days: int = Field(default=5, ge=1, le=100)
    start_date: date | None = None


class CaptionEditRequest(BaseModel):
    final_caption: str = Field(min_length=1, max_length=1000)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)
    note: str | None = Field(default=None, max_length=1000)
    image_verdict: str | None = None


class AlternativeRequest(BaseModel):
    index: int = Field(ge=0)


class RejectionRequest(BaseModel):
    reason: str = Field(default="not a fit", min_length=1, max_length=500)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)
    image_verdict: str | None = None


class CaptionFeedbackRequest(BaseModel):
    verdict: str
    reason_codes: list[str] = Field(default_factory=list, max_length=10)
    preferred_caption: str | None = Field(default=None, max_length=1000)
    preferred_structure: str | None = None
    image_verdict: str | None = None
    note: str | None = Field(default=None, max_length=1000)


class RightsReviewRequest(BaseModel):
    decision: str


class PublishConfirmationRequest(BaseModel):
    confirmation_token: str = Field(min_length=20, max_length=200)
    confirmation_phrase: str = Field(min_length=1, max_length=100)


class ReplacementRequest(BaseModel):
    candidate_id: int | None = None


class RescheduleRequest(BaseModel):
    new_date: date


class MetadataCorrectionRequest(BaseModel):
    fields: dict[str, object] = Field(min_length=1)


class SettingsUpdateRequest(BaseModel):
    fields: dict[str, Any] = Field(min_length=1)


class EditorialApproveRequest(BaseModel):
    final_caption: str = Field(min_length=1, max_length=1000)


class EditorialRejectRequest(BaseModel):
    reason: str = Field(default="not a fit", min_length=1, max_length=500)


class EnsureOptionsRequest(BaseModel):
    target: int = Field(default=5, ge=1, le=50)
    live_discovery: bool = True


def build_proposal_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["proposals"])
    proposals = ProposalService(database, settings)
    editorial = EditorialService(database, settings)
    youtube_publisher = YouTubeBrowserPublisher(database, settings)
    publisher_queue = PublisherQueueCoordinator(youtube_publisher)
    if settings.publishing_enabled:
        publisher_queue.start()

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

    @router.get("/editorial/next")
    def editorial_next() -> dict[str, object]:
        return {
            "next_proposal": proposals.next_for_review(),
            "workflow": proposals.workflow_summary(),
            "publisher_queue": publisher_queue.status(),
        }

    @router.get("/editorial/status")
    def editorial_status() -> dict[str, object]:
        return {
            "workflow": proposals.workflow_summary(),
            "publisher_queue": publisher_queue.status(),
        }

    @router.post("/editorial/options/ensure")
    def ensure_editorial_options(payload: EnsureOptionsRequest) -> dict[str, object]:
        try:
            return asyncio.run(
                editorial.ensure_options(
                    target=payload.target,
                    live_discovery=payload.live_discovery,
                )
            )
        except (LookupError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/editorial/proposals/{proposal_id}/approve")
    def editorial_approve(
        proposal_id: int,
        payload: EditorialApproveRequest,
    ) -> dict[str, object]:
        try:
            current = proposals.detail(proposal_id)
            if payload.final_caption.strip() != current["final_caption"]:
                proposals.edit_caption(
                    proposal_id,
                    payload.final_caption,
                    reason_codes=["human_edit"],
                    image_verdict="good",
                )
            proposals.approve(proposal_id)
            asyncio.run(InternalPublisher(database, settings).schedule_post(proposal_id))
            queue_result: dict[str, object] = {
                "running": False,
                "queued": 0,
                "paused": False,
                "paused_reason": None,
                "mode": "internal_only",
            }
            if settings.publishing_enabled:
                queue_result = {
                    **publisher_queue.enqueue(proposal_id),
                    "mode": "youtube",
                }
            return {
                "decision": "approved",
                "proposal": proposals.detail(proposal_id),
                "next_proposal": proposals.next_for_review(exclude_id=proposal_id),
                "workflow": proposals.workflow_summary(),
                "publisher_queue": queue_result,
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/editorial/proposals/{proposal_id}/reject")
    def editorial_reject(
        proposal_id: int,
        payload: EditorialRejectRequest,
    ) -> dict[str, object]:
        try:
            rejected = proposals.reject(
                proposal_id,
                payload.reason,
                reason_codes=["not_engaging"],
                image_verdict="unsure",
            )
            return {
                "decision": "rejected",
                "proposal": rejected,
                "next_proposal": proposals.next_for_review(exclude_id=proposal_id),
                "workflow": proposals.workflow_summary(),
                "publisher_queue": publisher_queue.status(),
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/editorial/proposals/{proposal_id}/skip-image")
    def editorial_skip_image(proposal_id: int) -> dict[str, object]:
        try:
            rejected = proposals.reject(
                proposal_id,
                "The image was not a fit.",
                reason_codes=["image_not_a_fit"],
                image_verdict="bad",
            )
            return {
                "decision": "image_rejected",
                "proposal": rejected,
                "next_proposal": proposals.next_for_review(exclude_id=proposal_id),
                "workflow": proposals.workflow_summary(),
                "publisher_queue": publisher_queue.status(),
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch("/proposals/{proposal_id}")
    def edit_caption(proposal_id: int, payload: CaptionEditRequest) -> dict[str, object]:
        try:
            return proposals.edit_caption(
                proposal_id,
                payload.final_caption,
                reason_codes=payload.reason_codes,
                note=payload.note,
                image_verdict=payload.image_verdict,
            )
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
            return proposals.reject(
                proposal_id,
                payload.reason,
                reason_codes=payload.reason_codes,
                image_verdict=payload.image_verdict,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/regenerate")
    def regenerate(proposal_id: int) -> dict[str, object]:
        try:
            return asyncio.run(proposals.regenerate_captions(proposal_id))
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/feedback")
    def record_feedback(
        proposal_id: int,
        payload: CaptionFeedbackRequest,
    ) -> dict[str, object]:
        try:
            return proposals.record_feedback(
                proposal_id,
                verdict=payload.verdict,
                reason_codes=payload.reason_codes,
                preferred_caption=payload.preferred_caption,
                preferred_structure=payload.preferred_structure,
                image_verdict=payload.image_verdict,
                note=payload.note,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/rights-review")
    def review_rights(
        proposal_id: int,
        payload: RightsReviewRequest,
    ) -> dict[str, object]:
        try:
            return proposals.review_rights(proposal_id, payload.decision)
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
            if detail["status"] not in {"needs_review", "approved", "rejected"}:
                raise ValueError("image blocking is unavailable after internal scheduling")
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
            if detail["status"] not in {"needs_review", "approved", "rejected"}:
                raise ValueError("domain blocking is unavailable after internal scheduling")
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

    @router.post("/publisher/session/validate")
    def validate_publisher_session() -> dict[str, object]:
        try:
            return asyncio.run(youtube_publisher.validate_session()).model_dump()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/publisher/queue")
    def publisher_queue_status() -> dict[str, object]:
        return publisher_queue.status()

    @router.post("/publisher/queue/resume")
    def publisher_queue_resume() -> dict[str, object]:
        try:
            return publisher_queue.resume()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/youtube/prepare")
    def prepare_youtube_schedule(proposal_id: int) -> dict[str, object]:
        try:
            return asyncio.run(youtube_publisher.prepare_attempt(proposal_id)).model_dump(
                mode="json"
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/publisher/attempts/{attempt_id}")
    def publish_attempt(attempt_id: int) -> dict[str, object]:
        try:
            return youtube_publisher.attempt_status(attempt_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/publisher/attempts/{attempt_id}/confirm")
    def confirm_youtube_schedule(
        attempt_id: int,
        payload: PublishConfirmationRequest,
    ) -> dict[str, object]:
        try:
            result = asyncio.run(
                youtube_publisher.confirm_schedule(
                    attempt_id,
                    confirmation_token=payload.confirmation_token,
                    confirmation_phrase=payload.confirmation_phrase,
                )
            )
            return {
                "result": result.model_dump(),
                "proposal": proposals.detail(result.proposal_id),
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/proposals/{proposal_id}/youtube/verify")
    def verify_youtube_schedule(proposal_id: int) -> dict[str, object]:
        try:
            result = asyncio.run(youtube_publisher.verify_scheduled_post(proposal_id))
            return {
                "result": result.model_dump(),
                "proposal": proposals.detail(proposal_id),
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/queue")
    def queue(
        days: int | None = Query(default=None, ge=1, le=500),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> dict[str, object]:
        return proposals.queue_status(days=days, limit=limit)

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
