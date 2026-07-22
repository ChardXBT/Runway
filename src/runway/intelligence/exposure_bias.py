from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CandidateExposure, CandidateImage, Proposal, SearchRun
from runway.db.repositories import audit, get_channel
from runway.intelligence.embeddings import ActiveRepresentationResolver


class CandidateExposureService:
    """Capture selection propensities and review events without inventing feedback."""

    selection_events = frozenset({"eligible_selected", "eligible_withheld"})
    review_events = frozenset(
        {
            "shown",
            "skipped",
            "replaced",
            "rejected",
            "accepted",
            "fewer_like_this",
        }
    )

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        resolver: ActiveRepresentationResolver | None = None,
    ):
        self.database = database
        self.settings = settings or database.settings
        self.representations = resolver or ActiveRepresentationResolver(database)

    def record_selection_pool(
        self,
        *,
        channel_id: int,
        candidate_ids: Sequence[int],
        selected_ids: Sequence[int],
        session_key: str,
        diagnostics: dict[str, object],
        exploration_policy: str = "deterministic_slate_v2",
        randomized: bool = False,
        display_probabilities: dict[int, float] | None = None,
    ) -> dict[str, object]:
        if not session_key.strip():
            raise ValueError("candidate exposure session key is required")
        pool_ids = list(dict.fromkeys(candidate_ids))
        selected_order = {candidate_id: rank for rank, candidate_id in enumerate(selected_ids, 1)}
        if not set(selected_order).issubset(pool_ids):
            raise ValueError("selected candidates must belong to the eligible pool")
        probabilities = display_probabilities or {}
        representation_sets = self.representations.snapshot(channel_id)
        raw_candidate_diagnostics = diagnostics.get("candidate_diagnostics", {})
        candidate_diagnostics = (
            raw_candidate_diagnostics if isinstance(raw_candidate_diagnostics, dict) else {}
        )
        created = 0
        with self.database.session() as session:
            rows = session.scalars(
                select(CandidateImage)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(
                    SearchRun.channel_id == channel_id,
                    CandidateImage.id.in_(pool_ids),
                )
            ).all()
            by_id = {row.id: row for row in rows}
            missing = [candidate_id for candidate_id in pool_ids if candidate_id not in by_id]
            if missing:
                raise ValueError(
                    "candidate exposure pool contains missing or cross-channel IDs: "
                    + ", ".join(str(value) for value in missing)
                )
            for candidate_id in pool_ids:
                candidate = by_id[candidate_id]
                selected = candidate_id in selected_order
                event_type = "eligible_selected" if selected else "eligible_withheld"
                raw_detail = candidate_diagnostics.get(str(candidate_id), {})
                detail = raw_detail if isinstance(raw_detail, dict) else {}
                default_probability = 1.0 if selected and not randomized else 0.0
                probability = self._probability(
                    probabilities.get(candidate_id, default_probability)
                )
                reason = self._text(detail.get("selection_reason"))
                withheld = self._reason_text(detail.get("suppression_reasons"))
                exposure = self._existing(
                    session,
                    candidate_id=candidate.id,
                    session_key=session_key,
                    event_type=event_type,
                )
                if exposure is None:
                    session.add(
                        CandidateExposure(
                            channel_id=channel_id,
                            candidate_image_id=candidate.id,
                            search_run_id=candidate.search_run_id,
                            caption_slate_id=None,
                            session_key=session_key,
                            event_type=event_type,
                            cluster_key=candidate.diversity_cluster_key,
                            pre_display_score=candidate.final_rank_score,
                            final_display_probability=probability,
                            display_position=selected_order.get(candidate.id),
                            exploration_policy=exploration_policy,
                            randomized=randomized,
                            eligible_pool_size=len(pool_ids),
                            selected_reason=(reason if selected else None),
                            withheld_reason=(withheld if not selected else None),
                            representation_sets_json=json.dumps(
                                representation_sets,
                                sort_keys=True,
                            ),
                        )
                    )
                    created += 1
                candidate.exploration_metadata_json = json.dumps(
                    {
                        "version": "candidate-exposure-v1",
                        "session_key": session_key,
                        "event_type": event_type,
                        "pre_display_score": candidate.final_rank_score,
                        "final_display_probability": probability,
                        "display_position": selected_order.get(candidate.id),
                        "exploration_policy": exploration_policy,
                        "randomized": randomized,
                        "eligible_pool_size": len(pool_ids),
                        "selection_diagnostics": detail,
                        "representation_sets": representation_sets,
                    },
                    sort_keys=True,
                )
            audit(
                session,
                "candidate_selection_exposure_recorded",
                "channel",
                channel_id,
                {
                    "session_key": session_key,
                    "eligible_pool_size": len(pool_ids),
                    "selected_ids": list(selected_ids),
                    "created": created,
                    "randomized": randomized,
                    "exploration_policy": exploration_policy,
                },
            )
        return {
            "created": created,
            "eligible_pool_size": len(pool_ids),
            "selected_count": len(selected_order),
            "randomized": randomized,
            "exploration_policy": exploration_policy,
        }

    def record_proposal_event(
        self,
        proposal_id: int,
        *,
        event_type: str,
        reason: str | None = None,
        session_key: str | None = None,
        candidate_image_id: int | None = None,
    ) -> bool:
        if event_type not in self.review_events:
            raise ValueError(f"unsupported candidate review exposure event: {event_type}")
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel_id,
                )
            )
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} was not found")
            target_candidate_id = candidate_image_id or proposal.candidate_image_id
            candidate = session.scalar(
                select(CandidateImage)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(
                    CandidateImage.id == target_candidate_id,
                    SearchRun.channel_id == channel_id,
                )
            )
            if candidate is None:
                raise LookupError(
                    f"candidate {target_candidate_id} was not found in the proposal channel"
                )
            candidate_id = candidate.id
            search_run_id = candidate.search_run_id
            caption_slate_id = proposal.caption_slate_id
            cluster_key = candidate.diversity_cluster_key
            pre_display_score = candidate.final_rank_score
        effective_session = session_key or f"review:proposal:{proposal_id}"
        representation_sets = self.representations.snapshot(channel_id)
        with self.database.session() as session:
            if (
                self._existing(
                    session,
                    candidate_id=candidate_id,
                    session_key=effective_session,
                    event_type=event_type,
                )
                is not None
            ):
                return False
            latest_selection = session.scalar(
                select(CandidateExposure)
                .where(
                    CandidateExposure.channel_id == channel_id,
                    CandidateExposure.candidate_image_id == candidate_id,
                    CandidateExposure.event_type.in_(self.selection_events),
                )
                .order_by(desc(CandidateExposure.created_at), desc(CandidateExposure.id))
                .limit(1)
            )
            session.add(
                CandidateExposure(
                    channel_id=channel_id,
                    candidate_image_id=candidate_id,
                    search_run_id=search_run_id,
                    caption_slate_id=caption_slate_id,
                    session_key=effective_session,
                    event_type=event_type,
                    cluster_key=cluster_key,
                    pre_display_score=pre_display_score,
                    final_display_probability=1.0,
                    display_position=1,
                    exploration_policy=(
                        latest_selection.exploration_policy
                        if latest_selection is not None
                        else "review_event"
                    ),
                    randomized=(latest_selection.randomized if latest_selection else False),
                    eligible_pool_size=(
                        latest_selection.eligible_pool_size if latest_selection is not None else 1
                    ),
                    selected_reason=reason or event_type,
                    withheld_reason=None,
                    representation_sets_json=json.dumps(
                        representation_sets,
                        sort_keys=True,
                    ),
                )
            )
            audit(
                session,
                "candidate_review_exposure_recorded",
                "proposal",
                proposal_id,
                {
                    "event_type": event_type,
                    "candidate_image_id": candidate_id,
                    "session_key": effective_session,
                },
            )
        return True

    def report(self) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            rows = session.scalars(
                select(CandidateExposure).where(CandidateExposure.channel_id == channel_id)
            ).all()
        event_counts = Counter(row.event_type for row in rows)
        shown_clusters = Counter(
            row.cluster_key or "unclustered"
            for row in rows
            if row.event_type in {"shown", "skipped", "replaced", "rejected", "accepted"}
        )
        shown_total = sum(shown_clusters.values())
        return {
            "version": "candidate-exposure-report-v1",
            "event_count": len(rows),
            "events": dict(sorted(event_counts.items())),
            "largest_cluster_share": (
                max(shown_clusters.values(), default=0) / shown_total if shown_total else None
            ),
            "cluster_count": len(shown_clusters),
            "propensity_ready": any(row.event_type in self.selection_events for row in rows),
            "off_policy_estimators": {
                "inverse_propensity_weighting": "evaluation_ready",
                "doubly_robust": "evaluation_ready_requires_outcome_model",
            },
        }

    @staticmethod
    def _existing(
        session: Session,
        *,
        candidate_id: int,
        session_key: str,
        event_type: str,
    ) -> CandidateExposure | None:
        return session.scalar(
            select(CandidateExposure)
            .where(
                CandidateExposure.candidate_image_id == candidate_id,
                CandidateExposure.session_key == session_key,
                CandidateExposure.event_type == event_type,
            )
            .limit(1)
        )

    @staticmethod
    def _probability(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("display probability must be numeric")
        probability = float(value)
        if not 0.0 <= probability <= 1.0:
            raise ValueError("display probability must be between zero and one")
        return probability

    @staticmethod
    def _text(value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    @classmethod
    def _reason_text(cls, value: object) -> str | None:
        if isinstance(value, list):
            text = ", ".join(str(item) for item in value if str(item).strip())
            return text or None
        return cls._text(value)
