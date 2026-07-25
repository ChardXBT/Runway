from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from runway.captions.exposures import CaptionExposureService
from runway.captions.feedback import CaptionFeedbackService
from runway.captions.service import CaptionService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    CaptionFeedback,
    CaptionSlate,
    GenerationRun,
    MediaAsset,
    Proposal,
    ProposalEvent,
    PublishAttempt,
    SearchRun,
    StyleProfile,
)
from runway.db.repositories import audit, get_channel
from runway.domain.enums import ProposalStatus, RunStatus
from runway.domain.state_machine import require_transition
from runway.intelligence.exposure_bias import CandidateExposureService
from runway.intelligence.retrieval import RetrievalService
from runway.intelligence.slate_optimization import CandidateSlateOptimizer
from runway.ranking.diversity import fingerprint_from_candidate

logger = logging.getLogger(__name__)


class NoDistinctCandidateError(ValueError):
    """The current unused candidate pool cannot satisfy the diversity policy."""


class ProposalService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.caption_service = CaptionService(database, settings)
        self.feedback = CaptionFeedbackService(database, settings)
        self.exposures = CaptionExposureService(database, settings)
        self.candidate_exposures = CandidateExposureService(database, settings)
        self.retrieval = RetrievalService(database, settings)
        self.diversity = CandidateSlateOptimizer(database, settings)
        self._schedule_lock = threading.Lock()

    async def generate_batch(
        self, *, days: int = 10, start_date: date | None = None
    ) -> dict[str, object]:
        if days < 1 or days > 500:
            raise ValueError("option count must be between 1 and 500")
        self._recover_stale_generation_runs()
        timezone_name, default_time = self._schedule_config()
        timezone = ZoneInfo(timezone_name)
        local_start = start_date or (datetime.now(timezone).date() + timedelta(days=1))
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            profile = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel.id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if profile is None:
                raise LookupError("build a style profile before generating proposals")
            accepted = session.scalars(
                select(CandidateImage)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(
                    SearchRun.channel_id == channel.id,
                    CandidateImage.hard_rejection_reason.is_(None),
                )
                .order_by(desc(CandidateImage.final_rank_score), CandidateImage.id)
            ).all()
            used_ids = set(
                session.scalars(
                    select(Proposal.candidate_image_id).where(Proposal.channel_id == channel.id)
                ).all()
            )
            recent_anchor_ids = session.scalars(
                select(Proposal.candidate_image_id)
                .where(Proposal.channel_id == channel.id)
                .order_by(Proposal.created_at.desc(), Proposal.id.desc())
                .limit(20)
            ).all()
            recent_franchise_candidates = session.scalars(
                select(CandidateImage)
                .join(Proposal, Proposal.candidate_image_id == CandidateImage.id)
                .where(Proposal.channel_id == channel.id)
                .order_by(Proposal.created_at.desc(), Proposal.id.desc())
                .limit(120)
            ).all()
            accepted_by_id = {candidate.id: candidate for candidate in accepted}
            available_candidates = [
                candidate for candidate in accepted if candidate.id not in used_ids
            ]
            anchor_candidates = [
                accepted_by_id[candidate_id]
                for candidate_id in recent_anchor_ids
                if candidate_id in accepted_by_id
            ]
            primary_franchise = self._primary_franchise(profile.profile_json)
            franchise_history = [
                fingerprint_from_candidate(candidate).franchise
                for candidate in reversed(recent_franchise_candidates)
            ]
            target_times = [
                self._planned_datetime(
                    local_start + timedelta(days=offset), timezone_name, default_time
                ).isoformat()
                for offset in range(days)
            ]
            occupied = set(
                session.scalars(
                    select(Proposal.planned_publish_at).where(
                        Proposal.channel_id == channel.id,
                        Proposal.planned_publish_at.in_(target_times),
                    )
                ).all()
            )
            missing_count = days - len(occupied)
            if not available_candidates and missing_count:
                raise NoDistinctCandidateError(
                    "no sufficiently distinct unused candidate is available; "
                    "Runway will not create repetitive filler"
                )
            run = GenerationRun(
                channel_id=channel.id,
                style_profile_id=profile.id,
                start_date=local_start,
                days=days,
                status=RunStatus.RUNNING.value,
                selection_diagnostics_json="{}",
            )
            session.add(run)
            session.flush()
            run_id = run.id
            channel_id = channel.id
            audit(
                session,
                "generation_started",
                "generation_run",
                run.id,
                {"days": days, "start_date": local_start.isoformat()},
            )

        created_ids: list[int] = []
        candidate_index = 0
        try:
            selection_slate = self.diversity.select(
                available_candidates,
                anchors=anchor_candidates,
                session_key=f"generation:{run_id}",
                primary_franchise=primary_franchise,
                minimum_primary_share=self.settings.discovery_primary_topic_query_share,
                franchise_history=franchise_history,
            )
            candidates = list(selection_slate.candidates)
            if not candidates and missing_count:
                raise NoDistinctCandidateError(
                    "no sufficiently distinct unused candidate is available after active-"
                    "representation slate optimization"
                )
            self.candidate_exposures.record_selection_pool(
                channel_id=channel_id,
                candidate_ids=[candidate.id for candidate in available_candidates],
                selected_ids=[candidate.id for candidate in candidates],
                session_key=f"generation:{run_id}",
                diagnostics=selection_slate.diagnostics,
                exploration_policy="deterministic_slate_v5",
                randomized=False,
            )
            with self.database.session() as session:
                loaded_run = session.get(GenerationRun, run_id)
                if loaded_run is not None:
                    loaded_run.selection_diagnostics_json = json.dumps(
                        selection_slate.diagnostics,
                        sort_keys=True,
                    )
            for offset in range(days):
                planned_date = local_start + timedelta(days=offset)
                planned_at = self._planned_datetime(planned_date, timezone_name, default_time)
                with self.database.session() as session:
                    existing = session.scalar(
                        select(Proposal).where(
                            Proposal.channel_id == channel_id,
                            Proposal.planned_publish_at == planned_at.isoformat(),
                        )
                    )
                if existing is not None:
                    created_ids.append(existing.id)
                    continue

                captions = None
                candidate = None
                while candidate_index < len(candidates):
                    current_candidate = candidates[candidate_index]
                    candidate_index += 1
                    current_captions = await self.caption_service.generate(current_candidate.id)
                    if current_captions.abstained:
                        continue
                    candidate = current_candidate
                    captions = current_captions
                    break
                if candidate is None or captions is None:
                    break
                context = self.retrieval.context_for_candidate(
                    candidate.media_asset_id,
                    candidate_id=candidate.id,
                )
                backup_ids: list[int] = []
                remaining_candidates = candidates[candidate_index:]
                if remaining_candidates:
                    backup_ids = [value.id for value in remaining_candidates[:2]]
                with self.database.session() as session:
                    current = session.get(CandidateImage, candidate.id)
                    if current is None or current.hard_rejection_reason:
                        raise ValueError(f"candidate {candidate.id} became unavailable")
                    proposal = Proposal(
                        generation_run_id=run_id,
                        channel_id=channel_id,
                        candidate_image_id=current.id,
                        caption_slate_id=captions.slate_id,
                        backup_candidate_ids_json=json.dumps(backup_ids),
                        planned_publish_at=planned_at.isoformat(),
                        recommended_caption=captions.recommended,
                        alternative_captions_json=json.dumps(captions.alternatives),
                        caption_rationale=captions.rationale,
                        caption_confidence=captions.confidence,
                        caption_reference_post_ids_json=json.dumps(
                            captions.referenced_historical_post_ids
                        ),
                        factual_uncertainty_warning=captions.factual_uncertainty_warning,
                        final_caption=captions.recommended,
                        status=ProposalStatus.GENERATING.value,
                        selection_reason=current.selection_reason,
                        style_score=current.style_score,
                        novelty_score=current.novelty_score,
                        quality_score=current.quality_score,
                        closest_historical_matches_json=json.dumps(
                            context["visual_examples"], default=str
                        ),
                        warnings_json=current.soft_warnings_json,
                    )
                    session.add(proposal)
                    session.flush()
                    if captions.slate_id is not None:
                        caption_slate = session.get(CaptionSlate, captions.slate_id)
                        if caption_slate is None or caption_slate.channel_id != channel_id:
                            raise ValueError("caption slate is missing or outside the channel")
                        caption_slate.proposal_id = proposal.id
                    self._event(
                        session,
                        proposal.id,
                        "proposal_created",
                        {},
                        {
                            "status": ProposalStatus.GENERATING.value,
                            "candidate_image_id": current.id,
                            "planned_publish_at": planned_at.isoformat(),
                        },
                    )
                    self._transition(
                        session, proposal, ProposalStatus.NEEDS_REVIEW, "generation_completed"
                    )
                    created_ids.append(proposal.id)
            target_met = len(created_ids) == days
            with self.database.session() as session:
                loaded_run = session.get(GenerationRun, run_id)
                if loaded_run:
                    loaded_run.status = (
                        RunStatus.COMPLETED.value if target_met else RunStatus.FAILED.value
                    )
                    loaded_run.completed_at = datetime.now(UTC)
                    loaded_run.error_summary = (
                        None
                        if target_met
                        else (
                            f"Generated {len(created_ids)} of {days} requested proposal dates; "
                            "the remaining safe slate was exhausted or abstained."
                        )
                    )
                    audit(
                        session,
                        "generation_completed" if target_met else "generation_partial",
                        "generation_run",
                        loaded_run.id,
                        {
                            "proposal_ids": created_ids,
                            "requested_count": days,
                            "fulfilled_count": len(created_ids),
                            "target_met": target_met,
                        },
                    )
        except Exception as exc:
            with self.database.session() as session:
                loaded_run = session.get(GenerationRun, run_id)
                if loaded_run:
                    loaded_run.status = RunStatus.FAILED.value
                    loaded_run.completed_at = datetime.now(UTC)
                    loaded_run.error_summary = f"{type(exc).__name__}: {exc}"
            raise
        return {
            "generation_run_id": run_id,
            "status": "completed" if target_met else "partial",
            "proposal_ids": created_ids,
            "start_date": local_start.isoformat(),
            "days": days,
            "requested_count": days,
            "fulfilled_count": len(created_ids),
            "unfilled_count": max(0, days - len(created_ids)),
            "timezone": timezone_name,
            "local_time": default_time,
        }

    def _recover_stale_generation_runs(self) -> int:
        """Close abandoned generation runs without touching an active long batch."""

        now = datetime.now(UTC)
        recovered = 0
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            rows = session.scalars(
                select(GenerationRun).where(
                    GenerationRun.channel_id == channel_id,
                    GenerationRun.status == RunStatus.RUNNING.value,
                )
            ).all()
            for row in rows:
                # Large batches can legitimately take many hours because every proposal
                # passes through discovery, multimodal verification, and caption review.
                # Keep the six-hour floor for short jobs, then budget three minutes per
                # requested option so a new request cannot kill a healthy long batch.
                abandonment_window = timedelta(
                    hours=6,
                    minutes=min(max(row.days, 1), 500) * 3,
                )
                started_at = row.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                else:
                    started_at = started_at.astimezone(UTC)
                if now - started_at <= abandonment_window:
                    continue
                row.status = RunStatus.FAILED.value
                row.completed_at = now
                row.error_summary = (
                    "Recovered abandoned generation run after its size-adjusted "
                    "completion window elapsed."
                )
                audit(
                    session,
                    "generation_stale_run_recovered",
                    "generation_run",
                    row.id,
                    {
                        "started_at": row.started_at.isoformat(),
                        "requested_count": row.days,
                        "abandonment_window_seconds": int(abandonment_window.total_seconds()),
                    },
                )
                recovered += 1
        return recovered

    def list_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        order: str = "asc",
    ) -> list[dict[str, object]]:
        if order not in {"asc", "desc"}:
            raise ValueError("proposal order must be 'asc' or 'desc'")
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            ordering = (
                (desc(Proposal.created_at), desc(Proposal.id))
                if order == "desc"
                else (Proposal.created_at, Proposal.id)
            )
            statement = (
                select(Proposal).where(Proposal.channel_id == channel_id).order_by(*ordering)
            )
            if status:
                statement = statement.where(Proposal.status == status)
            proposals = session.scalars(statement.limit(limit)).all()
            return [self._proposal_dict(session, proposal) for proposal in proposals]

    def next_for_review(self, *, exclude_id: int | None = None) -> dict[str, object] | None:
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            statement = (
                select(Proposal)
                .where(
                    Proposal.channel_id == channel_id,
                    Proposal.status == ProposalStatus.NEEDS_REVIEW.value,
                )
                .order_by(Proposal.created_at, Proposal.id)
                .limit(1)
            )
            if exclude_id is not None:
                statement = statement.where(Proposal.id != exclude_id)
            proposal = session.scalar(statement)
            result = self._proposal_dict(session, proposal) if proposal else None
            proposal_id = proposal.id if proposal else None
        if proposal_id is not None:
            try:
                self.exposures.record_display(proposal_id)
                self.candidate_exposures.record_proposal_event(
                    proposal_id,
                    event_type="shown",
                    reason="presented in the creator review interface",
                )
            except Exception:
                logger.exception(
                    "secondary display evidence failed for proposal %s",
                    proposal_id,
                )
        return result

    def next_generation_date(self) -> date:
        timezone_name, _default_time = self._schedule_config()
        timezone = ZoneInfo(timezone_name)
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            values = session.scalars(
                select(Proposal.planned_publish_at).where(Proposal.channel_id == channel_id)
            ).all()
        dates = [
            datetime.fromisoformat(value).astimezone(timezone).date() for value in values if value
        ]
        tomorrow = datetime.now(timezone).date() + timedelta(days=1)
        return max(dates, default=tomorrow - timedelta(days=1)) + timedelta(days=1)

    def detail(self, proposal_id: int) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            return self._proposal_dict(session, proposal, include_events=True)

    def generation_status(self, run_id: int) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            run = session.scalar(
                select(GenerationRun).where(
                    GenerationRun.id == run_id,
                    GenerationRun.channel_id == channel_id,
                )
            )
            if run is None:
                raise LookupError(f"generation run {run_id} not found")
            return {
                "id": run.id,
                "status": run.status,
                "start_date": run.start_date.isoformat(),
                "days": run.days,
                "started_at": run.started_at.isoformat(),
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "error_summary": run.error_summary,
            }

    def queue_status(
        self,
        *,
        days: int | None = None,
        start_date: date | None = None,
        limit: int = 5000,
    ) -> dict[str, object]:
        timezone_name, default_time = self._schedule_config()
        timezone = ZoneInfo(timezone_name)
        lineup_statuses = {
            ProposalStatus.APPROVED.value,
            ProposalStatus.INTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISHING.value,
            ProposalStatus.EXTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISH_UNVERIFIED.value,
            ProposalStatus.PUBLISH_FAILED.value,
        }
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            proposals = session.scalars(
                select(Proposal)
                .where(
                    Proposal.channel_id == channel_id,
                    Proposal.scheduled_publish_at.is_not(None),
                    Proposal.status.in_(lineup_statuses),
                )
                .order_by(Proposal.scheduled_publish_at, Proposal.id)
                .limit(limit)
            ).all()
            rows = [self._proposal_dict(session, proposal) for proposal in proposals]

        result: dict[str, object] = {
            "timezone": timezone_name,
            "default_time": default_time,
            "posts_per_day": 1,
            "scheduled": rows,
            "coverage": len(rows),
            "next_available_at": self.next_available_slot().isoformat(),
        }
        if days is None:
            return result

        local_start = start_date or (datetime.now(timezone).date() + timedelta(days=1))
        by_date: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            local_date = (
                datetime.fromisoformat(str(row["scheduled_publish_at"]))
                .astimezone(timezone)
                .date()
                .isoformat()
            )
            by_date.setdefault(local_date, []).append(row)
        calendar = []
        for offset in range(days):
            target = (local_start + timedelta(days=offset)).isoformat()
            active = [
                row
                for row in by_date.get(target, [])
                if row["status"]
                not in {
                    ProposalStatus.REJECTED.value,
                    ProposalStatus.CANCELLED.value,
                }
            ]
            calendar.append(
                {
                    "date": target,
                    "proposals": active,
                    "gap": not active,
                    "conflict": len(active) > 1,
                }
            )
        result.update(
            {
                "days": calendar,
                "coverage": sum(not day["gap"] for day in calendar),
                "gaps": [day["date"] for day in calendar if day["gap"]],
                "conflicts": [day["date"] for day in calendar if day["conflict"]],
            }
        )
        return result

    def workflow_summary(self) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            rows = session.scalars(select(Proposal).where(Proposal.channel_id == channel_id)).all()
        counts: dict[str, int] = {}
        for proposal in rows:
            counts[proposal.status] = counts.get(proposal.status, 0) + 1
        queued_statuses = {
            ProposalStatus.INTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISHING.value,
            ProposalStatus.PUBLISH_UNVERIFIED.value,
            ProposalStatus.PUBLISH_FAILED.value,
        }
        scheduled_statuses = {
            ProposalStatus.EXTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISHED.value,
        }
        return {
            "needs_review": counts.get(ProposalStatus.NEEDS_REVIEW.value, 0),
            "queued": sum(counts.get(status, 0) for status in queued_statuses),
            "scheduled": sum(counts.get(status, 0) for status in scheduled_statuses),
            "rejected": counts.get(ProposalStatus.REJECTED.value, 0),
            "next_available_at": self.next_available_slot().isoformat(),
            "posts_per_day": 1,
            "timezone": self._schedule_config()[0],
        }

    def next_available_slot(self, *, exclude_proposal_id: int | None = None) -> datetime:
        timezone_name, default_time = self._schedule_config()
        timezone = ZoneInfo(timezone_name)
        now = datetime.now(timezone)
        first_date = now.date()
        first_slot = self._planned_datetime(first_date, timezone_name, default_time)
        if first_slot <= now + timedelta(minutes=5):
            first_date += timedelta(days=1)
        reserving_statuses = {
            ProposalStatus.APPROVED.value,
            ProposalStatus.INTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISHING.value,
            ProposalStatus.EXTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISH_UNVERIFIED.value,
            ProposalStatus.PUBLISHED.value,
            ProposalStatus.PUBLISH_FAILED.value,
        }
        with self.database.session() as session:
            channel_id = self._channel_id(session)
            statement = select(Proposal.scheduled_publish_at).where(
                Proposal.channel_id == channel_id,
                Proposal.scheduled_publish_at.is_not(None),
                Proposal.status.in_(reserving_statuses),
            )
            if exclude_proposal_id is not None:
                statement = statement.where(Proposal.id != exclude_proposal_id)
            reserved = {
                datetime.fromisoformat(value).astimezone(timezone).date()
                for value in session.scalars(statement).all()
                if value
            }
        candidate_date = first_date
        while candidate_date in reserved:
            candidate_date += timedelta(days=1)
        return self._planned_datetime(candidate_date, timezone_name, default_time)

    def edit_caption(
        self,
        proposal_id: int,
        caption: str,
        *,
        reason_codes: list[str] | None = None,
        note: str | None = None,
        image_verdict: str | None = None,
    ) -> dict[str, object]:
        cleaned = caption.strip()
        if not cleaned:
            raise ValueError("final caption cannot be empty")
        unchanged = False
        decision_event_id: int | None = None
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {ProposalStatus.NEEDS_REVIEW, ProposalStatus.APPROVED},
                "caption editing",
            )
            old = proposal.final_caption
            if old == cleaned:
                unchanged = True
            else:
                if proposal.status == ProposalStatus.APPROVED.value:
                    self._release_schedule_slot(
                        session, proposal, "approval_slot_released_for_edit"
                    )
                    self._transition(
                        session,
                        proposal,
                        ProposalStatus.NEEDS_REVIEW,
                        "approval_reset_for_edit",
                    )
                    proposal.approved_at = None
                proposal.final_caption = cleaned
                event = self._event(
                    session,
                    proposal.id,
                    "caption_edited",
                    {"final_caption": old},
                    {"final_caption": cleaned},
                )
                decision_event_id = event.id
                audit(session, "caption_edited", "proposal", proposal.id, {})
        if unchanged:
            return self.detail(proposal_id)
        self.exposures.record_decision(
            proposal_id,
            decision_type="edited",
            final_caption=cleaned,
            original_caption=old,
            reason_codes=reason_codes or ["human_edit"],
            source_event_id=decision_event_id,
        )
        self.feedback.record(
            proposal_id,
            verdict="edited",
            generated_caption=old,
            preferred_caption=cleaned,
            reason_codes=reason_codes or ["human_edit"],
            image_verdict=image_verdict,
            note=note,
            source_event_id=decision_event_id,
        )
        return self.detail(proposal_id)

    def select_alternative(self, proposal_id: int, index: int) -> dict[str, object]:
        decision_event_id: int | None = None
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {ProposalStatus.NEEDS_REVIEW, ProposalStatus.APPROVED},
                "alternative selection",
            )
            alternatives = json.loads(proposal.alternative_captions_json)
            if index not in range(len(alternatives)):
                raise ValueError("alternative caption index is out of range")
            if proposal.status == ProposalStatus.APPROVED.value:
                self._release_schedule_slot(
                    session,
                    proposal,
                    "approval_slot_released_for_alternative",
                )
                self._transition(
                    session,
                    proposal,
                    ProposalStatus.NEEDS_REVIEW,
                    "approval_reset_for_alternative",
                )
                proposal.approved_at = None
            old = proposal.final_caption
            proposal.final_caption = alternatives[index]
            event = self._event(
                session,
                proposal.id,
                "alternative_selected",
                {"final_caption": old},
                {"index": index, "final_caption": proposal.final_caption},
            )
            decision_event_id = event.id
            selected = proposal.final_caption
        self.exposures.record_decision(
            proposal_id,
            decision_type="selected",
            final_caption=selected,
            original_caption=old,
            reason_codes=["selected_alternative"],
            source_event_id=decision_event_id,
        )
        self.feedback.record(
            proposal_id,
            verdict="selected",
            generated_caption=old,
            preferred_caption=selected,
            reason_codes=["selected_alternative"],
            source_event_id=decision_event_id,
        )
        return self.detail(proposal_id)

    def approve(self, proposal_id: int) -> dict[str, object]:
        decision_event_id: int | None = None
        with self._schedule_lock:
            scheduled_for = self.next_available_slot(exclude_proposal_id=proposal_id)
            with self.database.session() as session:
                proposal = self._get(session, proposal_id)
                if not proposal.final_caption.strip():
                    raise ValueError("proposal cannot be approved without a final caption")
                candidate = session.get(CandidateImage, proposal.candidate_image_id)
                media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
                if candidate is None or media is None:
                    raise ValueError("proposal cannot be approved without a local candidate image")
                old_slot = proposal.scheduled_publish_at
                proposal.scheduled_publish_at = scheduled_for.isoformat()
                self._event(
                    session,
                    proposal.id,
                    "schedule_slot_assigned",
                    {"scheduled_publish_at": old_slot},
                    {"scheduled_publish_at": proposal.scheduled_publish_at},
                )
                event = self._transition(
                    session,
                    proposal,
                    ProposalStatus.APPROVED,
                    "approved",
                )
                decision_event_id = event.id
                proposal.approved_at = datetime.now(UTC)
                proposal.rejected_at = None
                source = self.settings.resolved_data_dir / media.local_path
                extension = Path(media.local_path).suffix
                relative = Path("media") / "approved" / f"{media.sha256}{extension}"
                destination = self.settings.resolved_data_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(source, destination)
                media.kind = "approved"
                media.local_path = relative.as_posix()
                audit(
                    session,
                    "proposal_approved",
                    "proposal",
                    proposal.id,
                    {
                        "final_caption": proposal.final_caption,
                        "scheduled_publish_at": proposal.scheduled_publish_at,
                        "bot_posts_for_day": 1,
                    },
                )
                generated_caption = proposal.recommended_caption
                final_caption = proposal.final_caption
        self.exposures.record_decision(
            proposal_id,
            decision_type="accepted",
            final_caption=final_caption,
            original_caption=generated_caption,
            reason_codes=["approved"],
            source_event_id=decision_event_id,
        )
        self.feedback.record(
            proposal_id,
            verdict="accepted",
            generated_caption=generated_caption,
            preferred_caption=final_caption,
            reason_codes=["approved"],
            image_verdict="good",
            source_event_id=decision_event_id,
        )
        self.candidate_exposures.record_proposal_event(
            proposal_id,
            event_type="accepted",
            reason="creator approved the image-caption proposal",
        )
        return self.detail(proposal_id)

    def accept_to_lineup(self, proposal_id: int, final_caption: str) -> dict[str, object]:
        """Atomically accept one review proposal and place it in the local Lineup.

        The user-facing decision, assigned slot, approved media copy, and local Lineup state
        commit together. Preference/exposure bookkeeping is intentionally best-effort after that
        core transaction so a secondary learning failure can never make a completed acceptance
        look unsuccessful to the operator.
        """

        cleaned = final_caption.strip()
        if not cleaned:
            raise ValueError("proposal cannot be accepted without a final caption")

        edit_event_id: int | None = None
        approval_event_id: int | None = None
        original_caption = ""
        generated_caption = ""
        caption_changed = False

        with self._schedule_lock:
            scheduled_for = self.next_available_slot(exclude_proposal_id=proposal_id)
            with self.database.session() as session:
                proposal = self._get(session, proposal_id)
                self._require_status(
                    proposal,
                    {ProposalStatus.NEEDS_REVIEW},
                    "editorial acceptance",
                )
                candidate = session.get(CandidateImage, proposal.candidate_image_id)
                media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
                if candidate is None or media is None:
                    raise ValueError("proposal cannot be accepted without a local candidate image")
                source = self.settings.resolved_data_dir / media.local_path
                if not source.is_file():
                    raise ValueError(
                        "proposal cannot be accepted because its local image is missing"
                    )

                original_caption = proposal.final_caption
                generated_caption = proposal.recommended_caption
                caption_changed = cleaned != original_caption
                if caption_changed:
                    proposal.final_caption = cleaned
                    edit_event = self._event(
                        session,
                        proposal.id,
                        "caption_edited",
                        {"final_caption": original_caption},
                        {"final_caption": cleaned},
                    )
                    edit_event_id = edit_event.id
                    audit(
                        session,
                        "caption_edited",
                        "proposal",
                        proposal.id,
                        {"source": "editorial_accept"},
                    )

                old_slot = proposal.scheduled_publish_at
                proposal.scheduled_publish_at = scheduled_for.isoformat()
                self._event(
                    session,
                    proposal.id,
                    "schedule_slot_assigned",
                    {"scheduled_publish_at": old_slot},
                    {"scheduled_publish_at": proposal.scheduled_publish_at},
                )
                approval_event = self._transition(
                    session,
                    proposal,
                    ProposalStatus.APPROVED,
                    "approved",
                )
                approval_event_id = approval_event.id
                proposal.approved_at = datetime.now(UTC)
                proposal.rejected_at = None

                extension = Path(media.local_path).suffix
                relative = Path("media") / "approved" / f"{media.sha256}{extension}"
                destination = self.settings.resolved_data_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(source, destination)
                media.kind = "approved"
                media.local_path = relative.as_posix()

                self._transition(
                    session,
                    proposal,
                    ProposalStatus.INTERNALLY_SCHEDULED,
                    "internally_scheduled",
                )
                audit(
                    session,
                    "proposal_approved",
                    "proposal",
                    proposal.id,
                    {
                        "final_caption": proposal.final_caption,
                        "scheduled_publish_at": proposal.scheduled_publish_at,
                        "bot_posts_for_day": 1,
                    },
                )
                audit(
                    session,
                    "proposal_internally_scheduled",
                    "proposal",
                    proposal.id,
                    {"network_action": False, "source": "editorial_accept"},
                )

        try:
            if caption_changed:
                self.exposures.record_decision(
                    proposal_id,
                    decision_type="edited",
                    final_caption=cleaned,
                    original_caption=original_caption,
                    reason_codes=["human_edit"],
                    source_event_id=edit_event_id,
                )
                self.feedback.record(
                    proposal_id,
                    verdict="edited",
                    generated_caption=original_caption,
                    preferred_caption=cleaned,
                    reason_codes=["human_edit"],
                    image_verdict="good",
                    source_event_id=edit_event_id,
                )
            self.exposures.record_decision(
                proposal_id,
                decision_type="accepted",
                final_caption=cleaned,
                original_caption=generated_caption,
                reason_codes=["approved"],
                source_event_id=approval_event_id,
            )
            self.feedback.record(
                proposal_id,
                verdict="accepted",
                generated_caption=generated_caption,
                preferred_caption=cleaned,
                reason_codes=["approved"],
                image_verdict="good",
                source_event_id=approval_event_id,
            )
            self.candidate_exposures.record_proposal_event(
                proposal_id,
                event_type="accepted",
                reason="creator approved the image-caption proposal",
            )
        except Exception:
            logger.exception(
                "secondary acceptance evidence failed after proposal %s entered Lineup",
                proposal_id,
            )

        return self.detail(proposal_id)

    def reject(
        self,
        proposal_id: int,
        reason: str,
        *,
        reason_codes: list[str] | None = None,
        image_verdict: str | None = None,
    ) -> dict[str, object]:
        inferred_reasons = reason_codes or self._reason_codes(reason)
        # A validation failure must not commit the rejection without its
        # corresponding learning signal.
        self.feedback.validate_fields(
            verdict="rejected",
            reason_codes=inferred_reasons,
            image_verdict=image_verdict,
            note=reason,
        )
        decision_event_id: int | None = None
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._release_schedule_slot(session, proposal, "schedule_slot_released_for_rejection")
            self._transition(session, proposal, ProposalStatus.REJECTED, "rejected")
            proposal.rejected_at = datetime.now(UTC)
            event = self._event(
                session,
                proposal.id,
                "rejection_feedback",
                {},
                {
                    "reason": reason,
                    "candidate_image_id": proposal.candidate_image_id,
                    "caption": proposal.final_caption,
                },
            )
            decision_event_id = event.id
            audit(session, "proposal_rejected", "proposal", proposal.id, {"reason": reason})
            rejected_caption = proposal.final_caption
        self.exposures.record_decision(
            proposal_id,
            decision_type="rejected",
            final_caption=None,
            original_caption=rejected_caption,
            reason_codes=inferred_reasons,
            source_event_id=decision_event_id,
        )
        self.feedback.record(
            proposal_id,
            verdict="rejected",
            generated_caption=rejected_caption,
            reason_codes=inferred_reasons,
            image_verdict=image_verdict,
            note=reason,
            source_event_id=decision_event_id,
        )
        self.candidate_exposures.record_proposal_event(
            proposal_id,
            event_type="rejected",
            reason=reason,
        )
        return self.detail(proposal_id)

    def record_feedback(
        self,
        proposal_id: int,
        *,
        verdict: str,
        reason_codes: list[str] | None = None,
        preferred_caption: str | None = None,
        preferred_structure: str | None = None,
        image_verdict: str | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            event = self._event(
                session,
                proposal.id,
                "creator_feedback_recorded",
                {},
                {
                    "verdict": verdict,
                    "reason_codes": sorted(reason_codes or []),
                    "image_verdict": image_verdict,
                },
            )
            source_event_id = event.id
        self.feedback.record(
            proposal_id,
            verdict=verdict,
            preferred_caption=preferred_caption,
            preferred_structure=preferred_structure,
            reason_codes=reason_codes,
            image_verdict=image_verdict,
            note=note,
            source_event_id=source_event_id,
        )
        if "fewer_like_this" in (reason_codes or []):
            self.candidate_exposures.record_proposal_event(
                proposal_id,
                event_type="fewer_like_this",
                reason=note or "creator requested fewer candidates like this",
            )
        return self.detail(proposal_id)

    def review_rights(self, proposal_id: int, decision: str) -> dict[str, object]:
        if decision not in {"accepted_for_proposal", "blocked"}:
            raise ValueError("rights decision must be accepted_for_proposal or blocked")
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {ProposalStatus.NEEDS_REVIEW},
                "rights review",
            )
            proposal.rights_decision = decision
            proposal.rights_reviewed_at = datetime.now(UTC)
            self._event(
                session,
                proposal.id,
                "rights_reviewed",
                {},
                {"decision": decision},
            )
            audit(
                session,
                "proposal_rights_reviewed",
                "proposal",
                proposal.id,
                {"decision": decision},
            )
        return self.detail(proposal_id)

    async def regenerate_captions(self, proposal_id: int) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {ProposalStatus.NEEDS_REVIEW},
                "caption regeneration",
            )
            candidate_id = proposal.candidate_image_id
            final_before = proposal.final_caption
            final_was_user_edited = proposal.final_caption != proposal.recommended_caption
            generated_before = {
                "recommended": proposal.recommended_caption,
                "alternatives": json.loads(proposal.alternative_captions_json),
                "rationale": proposal.caption_rationale,
                "confidence": proposal.caption_confidence,
                "referenced_historical_post_ids": json.loads(
                    proposal.caption_reference_post_ids_json
                ),
                "factual_uncertainty_warning": proposal.factual_uncertainty_warning,
            }
        captions = await self.caption_service.generate(candidate_id)
        if captions.abstained:
            raise ValueError(
                "caption regeneration abstained; the existing caption slate was preserved"
            )
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            proposal.caption_slate_id = captions.slate_id
            if captions.slate_id is not None:
                slate = session.get(CaptionSlate, captions.slate_id)
                if slate is None or slate.channel_id != proposal.channel_id:
                    raise ValueError("regenerated caption slate is missing or outside the channel")
                slate.proposal_id = proposal.id
            proposal.recommended_caption = captions.recommended
            proposal.alternative_captions_json = json.dumps(captions.alternatives)
            proposal.caption_rationale = captions.rationale
            proposal.caption_confidence = captions.confidence
            proposal.caption_reference_post_ids_json = json.dumps(
                captions.referenced_historical_post_ids
            )
            proposal.factual_uncertainty_warning = captions.factual_uncertainty_warning
            # A user-edited final caption is authoritative and survives regeneration.
            proposal.final_caption = final_before if final_was_user_edited else captions.recommended
            self._event(
                session,
                proposal.id,
                "captions_regenerated",
                generated_before,
                {
                    "recommended": captions.recommended,
                    "alternatives": captions.alternatives,
                    "rationale": captions.rationale,
                    "confidence": captions.confidence,
                    "referenced_historical_post_ids": (captions.referenced_historical_post_ids),
                    "factual_uncertainty_warning": captions.factual_uncertainty_warning,
                    "final_caption_preserved": final_was_user_edited,
                },
            )
        self.exposures.record_display(proposal_id)
        return self.detail(proposal_id)

    async def replace_image(
        self, proposal_id: int, candidate_id: int | None = None
    ) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {
                    ProposalStatus.NEEDS_REVIEW,
                    ProposalStatus.REJECTED,
                    ProposalStatus.APPROVED,
                },
                "image replacement",
            )
            backups = [int(value) for value in json.loads(proposal.backup_candidate_ids_json)]
            replacement_id = candidate_id or (backups[0] if backups else None)
            if replacement_id is None:
                replacement = session.scalar(
                    select(CandidateImage)
                    .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                    .where(
                        SearchRun.channel_id == proposal.channel_id,
                        CandidateImage.hard_rejection_reason.is_(None),
                        CandidateImage.id != proposal.candidate_image_id,
                    )
                    .order_by(desc(CandidateImage.final_rank_score), CandidateImage.id)
                    .limit(1)
                )
                replacement_id = replacement.id if replacement else None
            replacement = (
                session.scalar(
                    select(CandidateImage)
                    .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                    .where(
                        CandidateImage.id == replacement_id,
                        SearchRun.channel_id == proposal.channel_id,
                    )
                )
                if replacement_id
                else None
            )
            already_used = (
                session.scalar(
                    select(Proposal.id).where(
                        Proposal.channel_id == proposal.channel_id,
                        Proposal.candidate_image_id == replacement_id,
                        Proposal.id != proposal.id,
                    )
                )
                if replacement_id
                else None
            )
            if already_used:
                replacement = session.scalar(
                    select(CandidateImage)
                    .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                    .where(
                        SearchRun.channel_id == proposal.channel_id,
                        CandidateImage.hard_rejection_reason.is_(None),
                        CandidateImage.id.not_in(
                            select(Proposal.candidate_image_id).where(
                                Proposal.channel_id == proposal.channel_id
                            )
                        ),
                    )
                    .order_by(desc(CandidateImage.final_rank_score), CandidateImage.id)
                    .limit(1)
                )
                replacement_id = replacement.id if replacement else None
            if replacement is None or replacement.hard_rejection_reason:
                raise ValueError("no accepted replacement candidate is available")
            selected_id = replacement.id
            old_candidate = proposal.candidate_image_id
        captions = await self.caption_service.generate(selected_id)
        if captions.abstained:
            raise ValueError(
                "replacement image produced no grounded caption slate; the proposal was preserved"
            )
        context = self.retrieval.context_for_candidate(
            replacement.media_asset_id,
            candidate_id=selected_id,
        )
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            if proposal.status == ProposalStatus.REJECTED.value:
                self._transition(
                    session, proposal, ProposalStatus.NEEDS_REVIEW, "replacement_requested"
                )
                proposal.rejected_at = None
            elif proposal.status == ProposalStatus.APPROVED.value:
                self._release_schedule_slot(
                    session,
                    proposal,
                    "approval_slot_released_for_replacement",
                )
                self._transition(
                    session, proposal, ProposalStatus.NEEDS_REVIEW, "approval_reset_for_replacement"
                )
                proposal.approved_at = None
            proposal.candidate_image_id = selected_id
            proposal.caption_slate_id = captions.slate_id
            if captions.slate_id is not None:
                slate = session.get(CaptionSlate, captions.slate_id)
                if slate is None or slate.channel_id != proposal.channel_id:
                    raise ValueError("replacement caption slate is missing or outside the channel")
                slate.proposal_id = proposal.id
            proposal.rights_decision = None
            proposal.rights_reviewed_at = None
            proposal.recommended_caption = captions.recommended
            proposal.alternative_captions_json = json.dumps(captions.alternatives)
            proposal.caption_rationale = captions.rationale
            proposal.caption_confidence = captions.confidence
            proposal.caption_reference_post_ids_json = json.dumps(
                captions.referenced_historical_post_ids
            )
            proposal.factual_uncertainty_warning = captions.factual_uncertainty_warning
            proposal.final_caption = captions.recommended
            proposal.style_score = replacement.style_score
            proposal.novelty_score = replacement.novelty_score
            proposal.quality_score = replacement.quality_score
            proposal.selection_reason = replacement.selection_reason
            proposal.closest_historical_matches_json = json.dumps(
                context["visual_examples"], default=str
            )
            remaining = [value for value in backups if value != selected_id]
            proposal.backup_candidate_ids_json = json.dumps(remaining)
            self._event(
                session,
                proposal.id,
                "image_replaced",
                {"candidate_image_id": old_candidate},
                {"candidate_image_id": selected_id},
            )
            audit(session, "proposal_image_replaced", "proposal", proposal.id, {})
        self.exposures.record_display(proposal_id)
        self.candidate_exposures.record_proposal_event(
            proposal_id,
            event_type="replaced",
            reason="creator replaced this image with another candidate",
            candidate_image_id=old_candidate,
        )
        self.candidate_exposures.record_proposal_event(
            proposal_id,
            event_type="shown",
            reason="replacement image presented in the creator review interface",
            candidate_image_id=selected_id,
        )
        return self.detail(proposal_id)

    def reschedule(self, proposal_id: int, new_date: date) -> dict[str, object]:
        timezone_name, default_time = self._schedule_config()
        planned = self._planned_datetime(new_date, timezone_name, default_time).isoformat()
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {ProposalStatus.NEEDS_REVIEW},
                "rescheduling",
            )
            conflict = session.scalar(
                select(Proposal.id).where(
                    Proposal.channel_id == proposal.channel_id,
                    Proposal.planned_publish_at == planned,
                    Proposal.id != proposal.id,
                    Proposal.status.not_in(
                        [ProposalStatus.REJECTED.value, ProposalStatus.CANCELLED.value]
                    ),
                )
            )
            if conflict:
                raise ValueError("another active proposal already occupies that date")
            old = proposal.planned_publish_at
            proposal.planned_publish_at = planned
            self._event(
                session,
                proposal.id,
                "proposal_rescheduled",
                {"planned_publish_at": old},
                {"planned_publish_at": planned},
            )
        return self.detail(proposal_id)

    def correct_candidate_metadata(
        self, proposal_id: int, fields: dict[str, object]
    ) -> dict[str, object]:
        allowed = {"franchise", "characters", "scene_archetype", "composition", "emotion"}
        if set(fields) - allowed:
            raise ValueError("metadata correction contains unsupported fields")
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._require_status(
                proposal,
                {ProposalStatus.NEEDS_REVIEW},
                "metadata correction",
            )
            candidate = session.get(CandidateImage, proposal.candidate_image_id)
            if candidate is None:
                raise LookupError("proposal candidate is missing")
            old = json.loads(candidate.detected_topic_json)
            updated = dict(old)
            updated.update(fields)
            candidate.detected_topic_json = json.dumps(updated, sort_keys=True)
            self._event(
                session,
                proposal.id,
                "candidate_metadata_corrected",
                old,
                fields,
            )
        return self.detail(proposal_id)

    def _planned_datetime(
        self,
        target_date: date,
        timezone_name: str | None = None,
        default_time: str | None = None,
    ) -> datetime:
        if timezone_name is None or default_time is None:
            timezone_name, default_time = self._schedule_config()
        hour, minute = (int(value) for value in default_time.split(":"))
        return datetime.combine(
            target_date,
            time(hour=hour, minute=minute),
            tzinfo=ZoneInfo(timezone_name),
        )

    def _schedule_config(self) -> tuple[str, str]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            return channel.timezone, channel.default_post_time

    @staticmethod
    def _primary_franchise(profile_json: str) -> str | None:
        try:
            profile = json.loads(profile_json)
        except (TypeError, json.JSONDecodeError):
            return None
        distribution = profile.get(
            "topic_distribution",
            profile.get("franchise_distribution", []),
        )
        if not isinstance(distribution, list):
            return None
        for entry in distribution:
            if not isinstance(entry, (list, tuple)) or not entry:
                continue
            franchise = str(entry[0] or "").strip()
            if franchise.casefold() not in {"", "unknown", "none", "null"}:
                return franchise
        return None

    @staticmethod
    def _reason_codes(reason: str) -> list[str]:
        lowered = reason.lower()
        codes: list[str] = []
        mappings = {
            "generic": "too_generic",
            "question": "prefer_open_question",
            "emotion": "wrong_emotion",
            "character": "wrong_character",
            "context": "invented_context",
            "engag": "not_engaging",
            "funny": "not_funny",
            "long": "too_long",
            "repet": "too_similar",
            "similar": "too_similar",
            "source": "source_concern",
            "rights": "source_concern",
        }
        for marker, code in mappings.items():
            if marker in lowered and code not in codes:
                codes.append(code)
        return codes or ["not_engaging"]

    def _channel_id(self, session: Session) -> int:
        return get_channel(session, self.settings.channel_handle).id

    def _get(self, session: Session, proposal_id: int) -> Proposal:
        proposal = session.scalar(
            select(Proposal).where(
                Proposal.id == proposal_id,
                Proposal.channel_id == self._channel_id(session),
            )
        )
        if proposal is None:
            raise LookupError(f"proposal {proposal_id} not found")
        return proposal

    @staticmethod
    def _require_status(
        proposal: Proposal,
        allowed: set[ProposalStatus],
        action: str,
    ) -> None:
        current = ProposalStatus(proposal.status)
        if current not in allowed:
            expected = ", ".join(sorted(status.value for status in allowed))
            raise ValueError(
                f"{action} requires proposal status {expected}; current status is {current.value}"
            )

    @staticmethod
    def _event(
        session: Session,
        proposal_id: int,
        event_type: str,
        old: dict[str, object],
        new: dict[str, object],
    ) -> ProposalEvent:
        event = ProposalEvent(
            proposal_id=proposal_id,
            event_type=event_type,
            old_value_json=json.dumps(old, sort_keys=True, default=str),
            new_value_json=json.dumps(new, sort_keys=True, default=str),
        )
        session.add(event)
        session.flush()
        return event

    def _transition(
        self,
        session: Session,
        proposal: Proposal,
        new_status: ProposalStatus,
        event_type: str,
    ) -> ProposalEvent:
        old_status = ProposalStatus(proposal.status)
        require_transition(old_status, new_status)
        proposal.status = new_status.value
        return self._event(
            session,
            proposal.id,
            event_type,
            {"status": old_status.value},
            {"status": new_status.value},
        )

    def _release_schedule_slot(
        self,
        session: Session,
        proposal: Proposal,
        event_type: str,
    ) -> None:
        if proposal.scheduled_publish_at is None:
            return
        old_slot = proposal.scheduled_publish_at
        proposal.scheduled_publish_at = None
        self._event(
            session,
            proposal.id,
            event_type,
            {"scheduled_publish_at": old_slot},
            {"scheduled_publish_at": None},
        )

    def _proposal_dict(
        self, session: Session, proposal: Proposal, *, include_events: bool = False
    ) -> dict[str, object]:
        candidate = session.get(CandidateImage, proposal.candidate_image_id)
        media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
        preview = (
            session.get(MediaAsset, candidate.preview_asset_id)
            if candidate and candidate.preview_asset_id
            else None
        )
        display_media = preview or media
        latest_publish_attempt = session.scalar(
            select(PublishAttempt)
            .where(PublishAttempt.proposal_id == proposal.id)
            .order_by(desc(PublishAttempt.id))
            .limit(1)
        )
        slate = (
            session.get(CaptionSlate, proposal.caption_slate_id)
            if proposal.caption_slate_id
            else None
        )
        result: dict[str, object] = {
            "id": proposal.id,
            "generation_run_id": proposal.generation_run_id,
            "caption_slate_id": proposal.caption_slate_id,
            "retrieval_run_id": slate.retrieval_run_id if slate else None,
            "caption_slate_status": slate.status if slate else None,
            "planned_publish_at": proposal.planned_publish_at,
            "scheduled_publish_at": proposal.scheduled_publish_at,
            "status": proposal.status,
            "candidate_image_id": proposal.candidate_image_id,
            "backup_candidate_ids": json.loads(proposal.backup_candidate_ids_json),
            "recommended_caption": proposal.recommended_caption,
            "alternative_captions": json.loads(proposal.alternative_captions_json),
            "caption_rationale": proposal.caption_rationale,
            "caption_confidence": proposal.caption_confidence,
            "caption_reference_post_ids": json.loads(proposal.caption_reference_post_ids_json),
            "factual_uncertainty_warning": proposal.factual_uncertainty_warning,
            "final_caption": proposal.final_caption,
            "selection_reason": proposal.selection_reason,
            "scores": {
                "style": proposal.style_score,
                "novelty": proposal.novelty_score,
                "quality": proposal.quality_score,
            },
            "closest_historical_matches": json.loads(proposal.closest_historical_matches_json),
            "warnings": json.loads(proposal.warnings_json),
            "rights_decision": proposal.rights_decision,
            "rights_reviewed_at": (
                proposal.rights_reviewed_at.isoformat() if proposal.rights_reviewed_at else None
            ),
            "approved_at": proposal.approved_at.isoformat() if proposal.approved_at else None,
            "rejected_at": proposal.rejected_at.isoformat() if proposal.rejected_at else None,
            "external_post_id": proposal.external_post_id,
            "external_post_url": proposal.external_post_url,
            "scheduled_verified_at": (
                proposal.scheduled_verified_at.isoformat()
                if proposal.scheduled_verified_at
                else None
            ),
            "latest_publish_attempt": (
                {
                    "id": latest_publish_attempt.id,
                    "status": latest_publish_attempt.status,
                    "error_summary": latest_publish_attempt.error_summary,
                    "prepared_at": latest_publish_attempt.prepared_at.isoformat(),
                    "submitted_at": (
                        latest_publish_attempt.submitted_at.isoformat()
                        if latest_publish_attempt.submitted_at
                        else None
                    ),
                    "completed_at": (
                        latest_publish_attempt.completed_at.isoformat()
                        if latest_publish_attempt.completed_at
                        else None
                    ),
                }
                if latest_publish_attempt is not None
                else None
            ),
            "candidate": (
                {
                    "original_url": (
                        f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                        if media
                        else None
                    ),
                    "preview_url": (
                        f"/media/{Path(display_media.local_path).relative_to('media').as_posix()}"
                        if display_media
                        else None
                    ),
                    "source_page_url": candidate.source_page_url,
                    "direct_image_url": candidate.direct_image_url,
                    "source_domain": candidate.source_domain,
                    "rights_status": candidate.rights_status,
                    "detected_topic": json.loads(candidate.detected_topic_json),
                    "diversity_cluster_key": candidate.diversity_cluster_key,
                    "diversity_fingerprint": json.loads(
                        candidate.diversity_fingerprint_json or "{}"
                    ),
                }
                if candidate
                else None
            ),
            "created_at": proposal.created_at.isoformat(),
            "updated_at": proposal.updated_at.isoformat(),
        }
        if include_events:
            events = session.scalars(
                select(ProposalEvent)
                .where(ProposalEvent.proposal_id == proposal.id)
                .order_by(ProposalEvent.created_at, ProposalEvent.id)
            ).all()
            result["events"] = [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "old": json.loads(event.old_value_json),
                    "new": json.loads(event.new_value_json),
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]
            feedback = session.scalars(
                select(CaptionFeedback)
                .where(CaptionFeedback.proposal_id == proposal.id)
                .order_by(CaptionFeedback.created_at, CaptionFeedback.id)
            ).all()
            result["caption_feedback"] = [
                {
                    "id": row.id,
                    "verdict": row.verdict,
                    "generated_caption": row.generated_caption,
                    "preferred_caption": row.preferred_caption,
                    "preferred_structure": row.preferred_structure,
                    "reason_codes": json.loads(row.reason_codes_json),
                    "image_verdict": row.image_verdict,
                    "note": row.note,
                    "created_at": row.created_at.isoformat(),
                }
                for row in feedback
            ]
        return result
