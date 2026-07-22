from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import delete, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    BlockedSource,
    CandidateImage,
    Proposal,
    ProposalEvent,
    SearchRun,
)
from runway.db.repositories import audit, get_channel
from runway.discovery.safety import is_known_adult_domain
from runway.intelligence.policies import ChannelPolicyService
from runway.ranking.diversity import (
    DiversityFingerprint,
    assign_diversity_fields,
    candidate_analysis,
    fingerprint_from_candidate,
    fingerprint_similarity,
)
from runway.ranking.service import CandidateRanker


class CandidateDiversityService:
    legacy_copyright_rejections = {
        "fan_art",
        "personal_artwork",
        "personal_artwork_source",
        "prominent_watermark",
        "rights_blocked",
    }
    rights_warning_markers = (
        "copyright",
        "fan art",
        "independently created artwork",
        "licensing",
        "personal artwork",
        "rights",
        "watermark",
    )

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def backfill(self, *, reconsider_legacy_policy: bool = True) -> dict[str, object]:
        updated = 0
        reconsidered = 0
        off_topic_corrected = 0
        quarantined_proposals = 0
        near_duplicate_review_quarantined = 0
        rights_warnings_removed = 0
        ranker = CandidateRanker(self.database, self.settings)
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            candidates = session.scalars(
                select(CandidateImage)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel_id)
                .order_by(CandidateImage.id)
            ).all()
            active_domain_blocks = {
                row.value.casefold()
                for row in session.scalars(
                    select(BlockedSource).where(BlockedSource.source_type == "domain")
                ).all()
            }
            for candidate in candidates:
                old_cluster = candidate.diversity_cluster_key
                fingerprint = assign_diversity_fields(candidate)
                if old_cluster != fingerprint.cluster_key:
                    updated += 1
                candidate_warnings = self._without_rights_warnings(
                    json.loads(candidate.soft_warnings_json)
                )
                previous_warning_count = len(json.loads(candidate.soft_warnings_json))
                rights_warnings_removed += previous_warning_count - len(candidate_warnings)
                candidate.soft_warnings_json = json.dumps(candidate_warnings, sort_keys=True)
                legacy_policy_rejection = (
                    candidate.hard_rejection_reason in self.legacy_copyright_rejections
                )
                stale_domain_rejection = (
                    candidate.hard_rejection_reason == "blocked_domain"
                    and (candidate.source_domain or "").casefold() not in active_domain_blocks
                    and not is_known_adult_domain(candidate.source_domain)
                )
                analysis = candidate_analysis(candidate)
                eligibility = ranker.topic_eligibility.evaluate(analysis)
                topic_eligible = not eligibility.hard_reject
                semantic_reconsideration = (
                    candidate.hard_rejection_reason == "off_topic" and topic_eligible
                )
                if (
                    reconsider_legacy_policy
                    and (
                        legacy_policy_rejection
                        or stale_domain_rejection
                        or semantic_reconsideration
                    )
                    and topic_eligible
                ):
                    components = json.loads(candidate.score_components_json)
                    candidate.hard_rejection_reason = None
                    candidate.source_risk_score = 0.0
                    candidate.final_rank_score = self._current_score(components)
                    components["hard_rejection_reason"] = None
                    components["source_risk_score"] = 0.0
                    components["topic_eligibility"] = eligibility.model_dump()
                    components["final_rank_score"] = candidate.final_rank_score
                    candidate.score_components_json = json.dumps(components, sort_keys=True)
                    candidate.selection_reason = (
                        "Eligible under the creator's NSFW-only content policy; "
                        f"diversity cluster {fingerprint.cluster_key}."
                    )
                    reconsidered += 1
                if candidate.hard_rejection_reason is None and not topic_eligible:
                    candidate.hard_rejection_reason = "off_topic"
                    candidate.final_rank_score = 0.0
                    components = json.loads(candidate.score_components_json)
                    components["hard_rejection_reason"] = "off_topic"
                    components["topic_eligibility"] = eligibility.model_dump()
                    components["final_rank_score"] = 0.0
                    candidate.score_components_json = json.dumps(components, sort_keys=True)
                    candidate.selection_reason = (
                        f"Rejected after semantic topic revalidation: {eligibility.reason}."
                    )
                    off_topic_corrected += 1
                    for proposal in session.scalars(
                        select(Proposal).where(
                            Proposal.candidate_image_id == candidate.id,
                            Proposal.status == "needs_review",
                            Proposal.approved_at.is_(None),
                        )
                    ).all():
                        old_status = proposal.status
                        proposal.status = "cancelled"
                        session.add(
                            ProposalEvent(
                                proposal_id=proposal.id,
                                event_type="candidate_topic_revalidated",
                                old_value_json=json.dumps({"status": old_status}),
                                new_value_json=json.dumps(
                                    {
                                        "status": "cancelled",
                                        "candidate_rejection": "off_topic",
                                    }
                                ),
                            )
                        )
                        quarantined_proposals += 1
            proposals = session.scalars(
                select(Proposal).where(Proposal.channel_id == channel_id)
            ).all()
            for proposal in proposals:
                proposal_warnings = json.loads(proposal.warnings_json)
                filtered_warnings = self._without_rights_warnings(proposal_warnings)
                rights_warnings_removed += len(proposal_warnings) - len(filtered_warnings)
                proposal.warnings_json = json.dumps(filtered_warnings, sort_keys=True)
                if self._is_rights_warning(proposal.factual_uncertainty_warning):
                    proposal.factual_uncertainty_warning = None
                    rights_warnings_removed += 1
            retained_review_fingerprints: list[DiversityFingerprint] = []
            review_rows = session.execute(
                select(Proposal, CandidateImage)
                .join(CandidateImage, CandidateImage.id == Proposal.candidate_image_id)
                .where(
                    Proposal.channel_id == channel_id,
                    Proposal.status == "needs_review",
                    CandidateImage.hard_rejection_reason.is_(None),
                )
                .order_by(Proposal.created_at, Proposal.id)
            ).all()
            for proposal, candidate in review_rows:
                fingerprint = fingerprint_from_candidate(candidate)
                if any(
                    fingerprint.concept_key == retained.concept_key
                    or fingerprint_similarity(fingerprint, retained) >= 0.88
                    for retained in retained_review_fingerprints
                ):
                    proposal.status = "cancelled"
                    session.add(
                        ProposalEvent(
                            proposal_id=proposal.id,
                            event_type="near_duplicate_option_quarantined",
                            old_value_json=json.dumps({"status": "needs_review"}),
                            new_value_json=json.dumps(
                                {
                                    "status": "cancelled",
                                    "similarity_threshold": 0.88,
                                    "concept_key": fingerprint.concept_key,
                                }
                            ),
                        )
                    )
                    near_duplicate_review_quarantined += 1
                else:
                    retained_review_fingerprints.append(fingerprint)
            audit(
                session,
                "candidate_diversity_backfilled",
                "channel",
                channel_id,
                {
                    "updated": updated,
                    "reconsidered_legacy_copyright_rejections": reconsidered,
                    "off_topic_corrected": off_topic_corrected,
                    "quarantined_unreviewed_proposals": quarantined_proposals,
                    "near_duplicate_review_quarantined": (near_duplicate_review_quarantined),
                    "rights_warnings_removed": rights_warnings_removed,
                },
            )
        return {
            "updated": updated,
            "reconsidered_legacy_copyright_rejections": reconsidered,
            "off_topic_corrected": off_topic_corrected,
            "quarantined_unreviewed_proposals": quarantined_proposals,
            "near_duplicate_review_quarantined": near_duplicate_review_quarantined,
            "rights_warnings_removed": rights_warnings_removed,
            **self.report(),
        }

    @classmethod
    def _is_rights_warning(cls, value: object) -> bool:
        if not isinstance(value, str):
            return False
        normalized = value.casefold()
        return any(marker in normalized for marker in cls.rights_warning_markers)

    @classmethod
    def _without_rights_warnings(cls, values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        return [
            value
            for value in values
            if isinstance(value, str) and not cls._is_rights_warning(value)
        ]

    def configure_creator_policy(self) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            blocked = session.scalars(select(BlockedSource)).all()
            removed = [
                row.value
                for row in blocked
                if row.source_type == "domain" and not is_known_adult_domain(row.value)
            ]
            if removed:
                session.execute(
                    delete(BlockedSource).where(
                        BlockedSource.source_type == "domain",
                        BlockedSource.value.in_(removed),
                    )
                )
            audit(
                session,
                "public_image_policy_configured",
                "channel",
                channel_id,
                {"removed_legacy_domain_blocks": sorted(removed)},
            )
        policy = ChannelPolicyService(self.database, self.settings).configure_public_image_policy(
            channel_id
        )
        return {
            "rights_policy": policy.rights_policy,
            "source_policy": policy.source_policy,
            "removed_legacy_domain_blocks": sorted(removed),
            "nsfw_blocked": True,
        }

    def report(self) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            rows = session.execute(
                select(
                    CandidateImage.id,
                    CandidateImage.diversity_cluster_key,
                    CandidateImage.hard_rejection_reason,
                )
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel_id)
            ).all()
            proposal_clusters = session.scalars(
                select(CandidateImage.diversity_cluster_key)
                .join(Proposal, Proposal.candidate_image_id == CandidateImage.id)
                .where(Proposal.channel_id == channel_id)
            ).all()
        accepted = [row for row in rows if row.hard_rejection_reason is None]
        accepted_clusters = Counter(row.diversity_cluster_key or "unclustered" for row in accepted)
        proposal_counts = Counter(value or "unclustered" for value in proposal_clusters)
        return {
            "candidate_count": len(rows),
            "accepted_candidate_count": len(accepted),
            "accepted_unique_clusters": len(accepted_clusters),
            "accepted_largest_cluster_size": max(accepted_clusters.values(), default=0),
            "proposal_unique_clusters": len(proposal_counts),
            "proposal_largest_cluster_size": max(proposal_counts.values(), default=0),
            "rejection_reasons": dict(
                sorted(
                    Counter(
                        row.hard_rejection_reason
                        for row in rows
                        if row.hard_rejection_reason is not None
                    ).items()
                )
            ),
        }

    @staticmethod
    def _current_score(components: dict[str, object]) -> float:
        weights = CandidateRanker.weights

        def value(name: str) -> float:
            raw = components.get(name, 0.0)
            return float(raw) if isinstance(raw, (int, float)) else 0.0

        score = (
            weights["style"] * value("style_score")
            + weights["topic"] * (0.8 if value("style_score") else 0.55)
            + weights["novelty"] * value("novelty_score")
            + weights["caption_potential"] * value("caption_potential_score")
            + weights["quality"] * value("quality_score")
            + weights["rotation"] * value("rotation_score")
            + weights["creator_image_preference"] * value("creator_image_preference_score")
            - value("text_overlay_penalty")
        )
        return round(max(0.0, min(1.0, score)), 6)
