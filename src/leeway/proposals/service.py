from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from leeway.captions.service import CaptionService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import (
    CandidateImage,
    GenerationRun,
    MediaAsset,
    Proposal,
    ProposalEvent,
    StyleProfile,
)
from leeway.db.repositories import audit, get_channel
from leeway.domain.enums import ProposalStatus, RunStatus
from leeway.domain.state_machine import require_transition
from leeway.intelligence.retrieval import RetrievalService


class ProposalService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.caption_service = CaptionService(database, settings)
        self.retrieval = RetrievalService(database, settings)

    async def generate_batch(
        self, *, days: int = 10, start_date: date | None = None
    ) -> dict[str, object]:
        if days < 1 or days > 30:
            raise ValueError("days must be between 1 and 30")
        timezone_name, default_time = self._schedule_config()
        timezone = ZoneInfo(timezone_name)
        local_start = start_date or (datetime.now(timezone).date() + timedelta(days=1))
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            profile = session.scalar(
                select(StyleProfile)
                .where(StyleProfile.is_active.is_(True))
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if profile is None:
                raise LookupError("build a style profile before generating proposals")
            accepted = session.scalars(
                select(CandidateImage)
                .where(CandidateImage.hard_rejection_reason.is_(None))
                .order_by(desc(CandidateImage.final_rank_score), CandidateImage.id)
            ).all()
            used_ids = set(session.scalars(select(Proposal.candidate_image_id)).all())
            candidates = [candidate for candidate in accepted if candidate.id not in used_ids]
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
            if len(candidates) < missing_count:
                raise ValueError(
                    f"{missing_count} queue gaps require {missing_count} "
                    "unused accepted candidates; "
                    f"only {len(candidates)} are available"
                )
            primary_candidates = candidates[:missing_count]
            reserve_candidates = candidates[missing_count:]
            run = GenerationRun(
                channel_id=channel.id,
                style_profile_id=profile.id,
                start_date=local_start,
                days=days,
                status=RunStatus.RUNNING.value,
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

                candidate = primary_candidates[candidate_index]
                candidate_index += 1
                captions = await self.caption_service.generate(candidate.id)
                context = self.retrieval.context_for_candidate(candidate.media_asset_id)
                backup_ids: list[int] = []
                if reserve_candidates:
                    for backup_offset in range(min(2, len(reserve_candidates))):
                        index = (candidate_index * 2 + backup_offset) % len(reserve_candidates)
                        backup_ids.append(reserve_candidates[index].id)
                with self.database.session() as session:
                    current = session.get(CandidateImage, candidate.id)
                    if current is None or current.hard_rejection_reason:
                        raise ValueError(f"candidate {candidate.id} became unavailable")
                    proposal = Proposal(
                        generation_run_id=run_id,
                        channel_id=channel_id,
                        candidate_image_id=current.id,
                        backup_candidate_ids_json=json.dumps(backup_ids),
                        planned_publish_at=planned_at.isoformat(),
                        recommended_caption=captions.recommended,
                        alternative_captions_json=json.dumps(captions.alternatives),
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
            with self.database.session() as session:
                loaded_run = session.get(GenerationRun, run_id)
                if loaded_run:
                    loaded_run.status = RunStatus.COMPLETED.value
                    loaded_run.completed_at = datetime.now(UTC)
                    audit(
                        session,
                        "generation_completed",
                        "generation_run",
                        loaded_run.id,
                        {"proposal_ids": created_ids},
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
            "status": "completed",
            "proposal_ids": created_ids,
            "start_date": local_start.isoformat(),
            "days": days,
            "timezone": timezone_name,
            "local_time": default_time,
        }

    def list_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(Proposal).order_by(Proposal.planned_publish_at, Proposal.id)
            if status:
                statement = statement.where(Proposal.status == status)
            proposals = session.scalars(statement.limit(limit)).all()
            return [self._proposal_dict(session, proposal) for proposal in proposals]

    def detail(self, proposal_id: int) -> dict[str, object]:
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            return self._proposal_dict(session, proposal, include_events=True)

    def generation_status(self, run_id: int) -> dict[str, object]:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
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

    def queue_status(self, *, days: int = 10, start_date: date | None = None) -> dict[str, object]:
        timezone_name, default_time = self._schedule_config()
        timezone = ZoneInfo(timezone_name)
        local_start = start_date or (datetime.now(timezone).date() + timedelta(days=1))
        rows = self.list_proposals(limit=500)
        by_date: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            local_date = (
                datetime.fromisoformat(str(row["planned_publish_at"]))
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
        return {
            "timezone": timezone_name,
            "default_time": default_time,
            "days": calendar,
            "coverage": sum(not day["gap"] for day in calendar),
            "gaps": [day["date"] for day in calendar if day["gap"]],
            "conflicts": [day["date"] for day in calendar if day["conflict"]],
        }

    def edit_caption(self, proposal_id: int, caption: str) -> dict[str, object]:
        cleaned = caption.strip()
        if not cleaned:
            raise ValueError("final caption cannot be empty")
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            if proposal.status == ProposalStatus.APPROVED.value:
                self._transition(
                    session, proposal, ProposalStatus.NEEDS_REVIEW, "approval_reset_for_edit"
                )
                proposal.approved_at = None
            old = proposal.final_caption
            proposal.final_caption = cleaned
            self._event(
                session,
                proposal.id,
                "caption_edited",
                {"final_caption": old},
                {"final_caption": cleaned},
            )
            audit(session, "caption_edited", "proposal", proposal.id, {})
        return self.detail(proposal_id)

    def select_alternative(self, proposal_id: int, index: int) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            alternatives = json.loads(proposal.alternative_captions_json)
            if index not in range(len(alternatives)):
                raise ValueError("alternative caption index is out of range")
            if proposal.status == ProposalStatus.APPROVED.value:
                self._transition(
                    session,
                    proposal,
                    ProposalStatus.NEEDS_REVIEW,
                    "approval_reset_for_alternative",
                )
                proposal.approved_at = None
            old = proposal.final_caption
            proposal.final_caption = alternatives[index]
            self._event(
                session,
                proposal.id,
                "alternative_selected",
                {"final_caption": old},
                {"index": index, "final_caption": proposal.final_caption},
            )
        return self.detail(proposal_id)

    def approve(self, proposal_id: int) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            if not proposal.final_caption.strip():
                raise ValueError("proposal cannot be approved without a final caption")
            self._transition(session, proposal, ProposalStatus.APPROVED, "approved")
            proposal.approved_at = datetime.now(UTC)
            proposal.rejected_at = None
            candidate = session.get(CandidateImage, proposal.candidate_image_id)
            media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
            if media:
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
                {"final_caption": proposal.final_caption},
            )
        return self.detail(proposal_id)

    def reject(self, proposal_id: int, reason: str) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            self._transition(session, proposal, ProposalStatus.REJECTED, "rejected")
            proposal.rejected_at = datetime.now(UTC)
            self._event(
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
            audit(session, "proposal_rejected", "proposal", proposal.id, {"reason": reason})
        return self.detail(proposal_id)

    async def regenerate_captions(self, proposal_id: int) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            candidate_id = proposal.candidate_image_id
            final_before = proposal.final_caption
            generated_before = {
                "recommended": proposal.recommended_caption,
                "alternatives": json.loads(proposal.alternative_captions_json),
            }
        captions = await self.caption_service.generate(candidate_id)
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            proposal.recommended_caption = captions.recommended
            proposal.alternative_captions_json = json.dumps(captions.alternatives)
            # A user-edited final caption is authoritative and survives regeneration.
            proposal.final_caption = final_before
            self._event(
                session,
                proposal.id,
                "captions_regenerated",
                generated_before,
                {
                    "recommended": captions.recommended,
                    "alternatives": captions.alternatives,
                    "final_caption_preserved": True,
                },
            )
        return self.detail(proposal_id)

    async def replace_image(
        self, proposal_id: int, candidate_id: int | None = None
    ) -> dict[str, object]:
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            backups = [int(value) for value in json.loads(proposal.backup_candidate_ids_json)]
            replacement_id = candidate_id or (backups[0] if backups else None)
            if replacement_id is None:
                replacement = session.scalar(
                    select(CandidateImage)
                    .where(
                        CandidateImage.hard_rejection_reason.is_(None),
                        CandidateImage.id != proposal.candidate_image_id,
                    )
                    .order_by(desc(CandidateImage.final_rank_score), CandidateImage.id)
                    .limit(1)
                )
                replacement_id = replacement.id if replacement else None
            replacement = session.get(CandidateImage, replacement_id) if replacement_id else None
            already_used = (
                session.scalar(
                    select(Proposal.id).where(
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
                    .where(
                        CandidateImage.hard_rejection_reason.is_(None),
                        CandidateImage.id.not_in(select(Proposal.candidate_image_id)),
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
        context = self.retrieval.context_for_candidate(replacement.media_asset_id)
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
            if proposal.status == ProposalStatus.REJECTED.value:
                self._transition(
                    session, proposal, ProposalStatus.NEEDS_REVIEW, "replacement_requested"
                )
                proposal.rejected_at = None
            elif proposal.status == ProposalStatus.APPROVED.value:
                self._transition(
                    session, proposal, ProposalStatus.NEEDS_REVIEW, "approval_reset_for_replacement"
                )
                proposal.approved_at = None
            proposal.candidate_image_id = selected_id
            proposal.recommended_caption = captions.recommended
            proposal.alternative_captions_json = json.dumps(captions.alternatives)
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
        return self.detail(proposal_id)

    def reschedule(self, proposal_id: int, new_date: date) -> dict[str, object]:
        timezone_name, default_time = self._schedule_config()
        planned = self._planned_datetime(new_date, timezone_name, default_time).isoformat()
        with self.database.session() as session:
            proposal = self._get(session, proposal_id)
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
    def _get(session: Session, proposal_id: int) -> Proposal:
        proposal = session.get(Proposal, proposal_id)
        if proposal is None:
            raise LookupError(f"proposal {proposal_id} not found")
        return proposal

    @staticmethod
    def _event(
        session: Session,
        proposal_id: int,
        event_type: str,
        old: dict[str, object],
        new: dict[str, object],
    ) -> None:
        session.add(
            ProposalEvent(
                proposal_id=proposal_id,
                event_type=event_type,
                old_value_json=json.dumps(old, sort_keys=True, default=str),
                new_value_json=json.dumps(new, sort_keys=True, default=str),
            )
        )

    def _transition(
        self,
        session: Session,
        proposal: Proposal,
        new_status: ProposalStatus,
        event_type: str,
    ) -> None:
        old_status = ProposalStatus(proposal.status)
        require_transition(old_status, new_status)
        proposal.status = new_status.value
        self._event(
            session,
            proposal.id,
            event_type,
            {"status": old_status.value},
            {"status": new_status.value},
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
        result: dict[str, object] = {
            "id": proposal.id,
            "generation_run_id": proposal.generation_run_id,
            "planned_publish_at": proposal.planned_publish_at,
            "status": proposal.status,
            "candidate_image_id": proposal.candidate_image_id,
            "backup_candidate_ids": json.loads(proposal.backup_candidate_ids_json),
            "recommended_caption": proposal.recommended_caption,
            "alternative_captions": json.loads(proposal.alternative_captions_json),
            "final_caption": proposal.final_caption,
            "selection_reason": proposal.selection_reason,
            "scores": {
                "style": proposal.style_score,
                "novelty": proposal.novelty_score,
                "quality": proposal.quality_score,
            },
            "closest_historical_matches": json.loads(proposal.closest_historical_matches_json),
            "warnings": json.loads(proposal.warnings_json),
            "approved_at": proposal.approved_at.isoformat() if proposal.approved_at else None,
            "rejected_at": proposal.rejected_at.isoformat() if proposal.rejected_at else None,
            "candidate": (
                {
                    "original_url": (
                        f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                        if media
                        else None
                    ),
                    "preview_url": (
                        f"/media/{Path(preview.local_path).relative_to('media').as_posix()}"
                        if preview
                        else None
                    ),
                    "source_page_url": candidate.source_page_url,
                    "direct_image_url": candidate.direct_image_url,
                    "source_domain": candidate.source_domain,
                    "rights_status": candidate.rights_status,
                    "detected_topic": json.loads(candidate.detected_topic_json),
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
        return result
