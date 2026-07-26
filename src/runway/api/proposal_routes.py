from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from runway.audit.service import AuditService
from runway.config import Settings
from runway.db.base import Database
from runway.discovery.service import DiscoveryService
from runway.editorial.service import EditorialService
from runway.proposals.service import ProposalService
from runway.publishing.internal import InternalPublisher
from runway.publishing.queue import PublisherQueueCoordinator
from runway.publishing.youtube import YouTubeBrowserPublisher
from runway.services.settings import SettingsService


class GenerationRequest(BaseModel):
    days: int = Field(default=5, ge=1, le=500)
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


class ConnectorAccessRequest(BaseModel):
    channel_url: str = Field(min_length=2, max_length=500)


class ReplacementRequest(BaseModel):
    candidate_id: int | None = None


class RescheduleRequest(BaseModel):
    new_date: date


class LineupUpdateRequest(BaseModel):
    final_caption: str | None = Field(default=None, min_length=1, max_length=1000)
    scheduled_publish_at: datetime | None = None
    new_scheduled_publish_at: datetime | None = None
    new_date: date | None = None
    confirmed: Literal[True]

    @model_validator(mode="after")
    def one_schedule_value(self) -> LineupUpdateRequest:
        supplied = sum(
            value is not None
            for value in (
                self.scheduled_publish_at,
                self.new_scheduled_publish_at,
                self.new_date,
            )
        )
        if supplied > 1:
            raise ValueError("send only one schedule timestamp or legacy date")
        return self


class LineupRemoveRequest(BaseModel):
    confirmed: Literal[True]


class LineupPushRequest(BaseModel):
    confirmed: Literal[True]
    mode: Literal["assisted", "authorized_browser"]
    proposal_ids: list[int] | None = Field(default=None, max_length=5000)


class MetadataCorrectionRequest(BaseModel):
    fields: dict[str, object] = Field(min_length=1)


class SettingsUpdateRequest(BaseModel):
    fields: dict[str, Any] = Field(min_length=1)


class EditorialApproveRequest(BaseModel):
    final_caption: str = Field(min_length=1, max_length=1000)


class EditorialRejectRequest(BaseModel):
    reason: str = Field(default="not a fit", min_length=1, max_length=500)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)


class EnsureOptionsRequest(BaseModel):
    target: int = Field(default=5, ge=1, le=50)
    live_discovery: bool = True


def build_proposal_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["proposals"])
    proposals = ProposalService(database, settings)
    editorial = EditorialService(database, settings)
    youtube_publisher = YouTubeBrowserPublisher(database, settings)
    publisher_queue = PublisherQueueCoordinator(youtube_publisher)
    if settings.authorized_browser_ready:
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
        order: Literal["asc", "desc"] = "asc",
    ) -> list[dict[str, object]]:
        return proposals.list_proposals(status=status, limit=limit, order=order)

    @router.get("/proposals/{proposal_id}")
    def proposal_detail(proposal_id: int) -> dict[str, object]:
        try:
            return proposals.detail(proposal_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/editorial/next")
    def editorial_next(
        proposal_id: int | None = Query(default=None, ge=1),
    ) -> dict[str, object]:
        return {
            "next_proposal": proposals.next_for_review(proposal_id=proposal_id),
            "workflow": proposals.workflow_summary(),
            "publisher_queue": publisher_queue.status(),
            "generation": editorial.generation_status(),
        }

    @router.get("/editorial/status")
    def editorial_status() -> dict[str, object]:
        return {
            "workflow": proposals.workflow_summary(),
            "publisher_queue": publisher_queue.status(),
            "generation": editorial.generation_status(),
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
            accepted = proposals.accept_to_lineup(proposal_id, payload.final_caption)
            return {
                "decision": "approved",
                "detail": "Accepted and added to Lineup. Nothing was sent to YouTube.",
                "proposal": accepted,
                "next_proposal": proposals.next_for_review(exclude_id=proposal_id),
                "workflow": proposals.workflow_summary(),
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
                reason_codes=payload.reason_codes or ["not_engaging"],
                image_verdict="bad",
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
            asyncio.run(youtube_publisher.validate_session())
            return youtube_publisher.connection_status()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/publisher/session/status")
    def publisher_session_status() -> dict[str, object]:
        return youtube_publisher.connection_status()

    @router.post("/publisher/connectors/validate")
    def validate_connector_access(
        payload: ConnectorAccessRequest,
    ) -> dict[str, object]:
        try:
            return asyncio.run(
                youtube_publisher.validate_channel_access(payload.channel_url)
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        limit: int = Query(default=5000, ge=1, le=10000),
    ) -> dict[str, object]:
        return proposals.queue_status(days=days, limit=limit)

    @router.post("/lineup/push")
    async def push_lineup_to_youtube(
        payload: LineupPushRequest,
    ) -> dict[str, object]:
        try:
            if payload.mode != settings.publishing_mode:
                raise ValueError(
                    f"requested mode {payload.mode!r} does not match configured "
                    f"RUNWAY_PUBLISHING_MODE={settings.publishing_mode}"
                )
            lineup = proposals.queue_status()
            scheduled_value = lineup.get("scheduled")
            scheduled = scheduled_value if isinstance(scheduled_value, list) else []
            eligible_ids = [
                int(item["id"])
                for item in scheduled
                if isinstance(item, dict)
                and item.get("status") in {"internally_scheduled", "publish_failed"}
                and isinstance(item.get("id"), int)
            ]
            if payload.proposal_ids == []:
                raise ValueError("select at least one Lineup post for external handling")
            selected_ids = list(
                dict.fromkeys(
                    eligible_ids if payload.proposal_ids is None else payload.proposal_ids
                )
            )
            ineligible = [
                proposal_id for proposal_id in selected_ids if proposal_id not in eligible_ids
            ]
            if ineligible:
                raise ValueError(
                    "selected posts are no longer eligible for external handling: "
                    + ", ".join(str(value) for value in ineligible)
                )
            if not selected_ids:
                return {
                    "mode": payload.mode,
                    "detail": "No Lineup posts need external handling.",
                    "queued_proposal_ids": [],
                    "lineup": lineup,
                    "publisher_queue": publisher_queue.status(),
                }

            if payload.mode == "assisted":
                workspace = youtube_publisher.prepare_assisted_batch(selected_ids)
                return {
                    "mode": "assisted",
                    "detail": (
                        f"Prepared {len(workspace.items)} validated "
                        f"{'post' if len(workspace.items) == 1 else 'posts'} for native YouTube."
                    ),
                    "queued_proposal_ids": [],
                    "assisted_workspace": workspace.model_dump(mode="json"),
                    "lineup": lineup,
                    "publisher_queue": publisher_queue.status(),
                }

            connection = youtube_publisher.connection_status()
            if connection.get("state") != "connected" or connection.get("valid") is not True:
                raise ValueError(
                    "authorized browser publishing requires a fresh successful capability check"
                )
            queued = publisher_queue.enqueue_many(selected_ids)
            return {
                "mode": "authorized_browser",
                "detail": (
                    f"{len(selected_ids)} Lineup "
                    f"{'post was' if len(selected_ids) == 1 else 'posts were'} "
                    "queued for authorized browser handling. External scheduling is not yet "
                    "confirmed."
                ),
                "queued_proposal_ids": selected_ids,
                "lineup": proposals.queue_status(),
                "publisher_queue": queued,
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.patch("/lineup/{proposal_id}")
    def update_lineup(
        proposal_id: int,
        payload: LineupUpdateRequest,
    ) -> dict[str, object]:
        try:
            operation = asyncio.run(
                youtube_publisher.update_lineup(
                    proposal_id,
                    final_caption=payload.final_caption,
                    new_scheduled_publish_at=(
                        payload.scheduled_publish_at or payload.new_scheduled_publish_at
                    ),
                    new_date=payload.new_date,
                )
            )
            return {
                "operation": operation,
                "lineup": proposals.queue_status(),
                "publisher_queue": publisher_queue.status(),
            }
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/lineup/{proposal_id}/remove")
    def remove_from_lineup(
        proposal_id: int,
        _payload: LineupRemoveRequest,
    ) -> dict[str, object]:
        try:
            operation = asyncio.run(youtube_publisher.remove_from_lineup(proposal_id))
            return {
                "operation": operation,
                "lineup": proposals.queue_status(),
                "publisher_queue": publisher_queue.status(),
            }
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/lineup/{proposal_id}/retry")
    def retry_lineup_publish(proposal_id: int) -> dict[str, object]:
        try:
            proposal = proposals.detail(proposal_id)
            latest_attempt = youtube_publisher.latest_attempt(proposal_id)
            if (
                proposal["status"] != "publish_failed"
                or latest_attempt is None
                or latest_attempt.get("status") != "failed_before_submission"
            ):
                raise ValueError(
                    "only a confirmed failed-before-submission YouTube action can be retried"
                )
            queued = publisher_queue.retry_failed(proposal_id)
            return {
                "proposal": proposals.detail(proposal_id),
                "lineup": proposals.queue_status(),
                "publisher_queue": queued,
            }
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/activity")
    def activity(
        event_type: str | None = None,
        entity_type: str | None = None,
        category: Literal[
            "all", "decisions", "publishing", "intelligence", "data", "settings", "other"
        ] = "all",
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, object]]:
        return AuditService(database).list_events(
            event_type=event_type,
            entity_type=entity_type,
            category=category,
            limit=limit,
            offset=offset,
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
