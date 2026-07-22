from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.analysis.schemas import CaptionCandidate
from runway.captions.feature_snapshots import (
    FEATURE_SCHEMA_VERSION,
    TAXONOMY_VERSION,
    VERIFIER_VERSION,
    candidate_components,
    snapshot_json_and_hash,
)
from runway.captions.planning import EditorialBrief
from runway.captions.taxonomy import analyze_caption
from runway.captions.verification import CaptionVerifier
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CaptionCandidateRecord,
    CaptionExposure,
    CaptionSlate,
    IntelligenceRetrievalRun,
    PairwisePreference,
    Proposal,
    ProposalEvent,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import (
    ActiveRepresentationResolver,
    RepresentationStore,
    TextEmbeddingProvider,
    configuration_hash,
    content_hash,
)
from runway.intelligence.policies import ChannelPolicyService


class CaptionExposureService:
    interface_version = "runway-generator-v2"

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.representations = ActiveRepresentationResolver(database)

    def record_display(self, proposal_id: int) -> CaptionExposure | None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if proposal.caption_slate_id is None:
                return None
            existing = session.scalar(
                select(CaptionExposure)
                .where(
                    CaptionExposure.proposal_id == proposal.id,
                    CaptionExposure.caption_slate_id == proposal.caption_slate_id,
                )
                .limit(1)
            )
            if existing is not None:
                return existing
            candidates = session.scalars(
                select(CaptionCandidateRecord)
                .where(
                    CaptionCandidateRecord.caption_slate_id == proposal.caption_slate_id,
                    CaptionCandidateRecord.displayed.is_(True),
                )
                .order_by(
                    CaptionCandidateRecord.display_order,
                    CaptionCandidateRecord.rank,
                )
            ).all()
            exposure = CaptionExposure(
                channel_id=proposal.channel_id,
                proposal_id=proposal.id,
                caption_slate_id=proposal.caption_slate_id,
                candidate_image_id=proposal.candidate_image_id,
                ordered_candidate_ids_json=json.dumps([row.id for row in candidates]),
                interface_version=self.interface_version,
            )
            session.add(exposure)
            session.flush()
            return exposure

    def record_decision(
        self,
        proposal_id: int,
        *,
        decision_type: str,
        final_caption: str | None,
        original_caption: str | None = None,
        reason_codes: list[str] | None = None,
        source_event_id: int | None = None,
    ) -> None:
        self.record_display(proposal_id)
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if proposal is None or proposal.caption_slate_id is None:
                return
            channel_id = proposal.channel_id
        policy = ChannelPolicyService(
            self.database,
            self.settings,
        ).ensure_defaults(channel_id)
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if proposal is None or proposal.caption_slate_id is None:
                return
            exposure = session.scalar(
                select(CaptionExposure)
                .where(
                    CaptionExposure.proposal_id == proposal.id,
                    CaptionExposure.caption_slate_id == proposal.caption_slate_id,
                )
                .limit(1)
            )
            if exposure is None:
                return
            candidate_ids = [
                int(value) for value in json.loads(exposure.ordered_candidate_ids_json)
            ]
            displayed_candidates = {
                row.id: row
                for row in session.scalars(
                    select(CaptionCandidateRecord).where(
                        CaptionCandidateRecord.id.in_(candidate_ids)
                    )
                )
            }
            slate_candidates = session.scalars(
                select(CaptionCandidateRecord)
                .where(CaptionCandidateRecord.caption_slate_id == proposal.caption_slate_id)
                .order_by(CaptionCandidateRecord.rank)
            ).all()
            candidates = {row.id: row for row in slate_candidates}
            source_event = (
                session.get(ProposalEvent, source_event_id) if source_event_id is not None else None
            )
            if source_event_id is not None and (
                source_event is None or source_event.proposal_id != proposal.id
            ):
                raise ValueError("decision source event does not belong to the proposal")
            source_event_key = (
                f"proposal-event:{source_event_id}"
                if source_event_id is not None
                else self._legacy_source_key(
                    exposure.id,
                    decision_type,
                    final_caption,
                    original_caption,
                )
            )
            selected = next(
                (
                    row
                    for row in candidates.values()
                    if final_caption is not None and row.text == final_caption.strip()
                ),
                None,
            )
            original = next(
                (
                    row
                    for row in candidates.values()
                    if original_caption is not None and row.text == original_caption.strip()
                ),
                None,
            )
            if (
                decision_type == "edited"
                and final_caption
                and original_caption
                and final_caption.strip() != original_caption.strip()
            ):
                selected = self._human_edit_candidate(
                    session=session,
                    proposal=proposal,
                    source_event_id=source_event_id,
                    source_event_key=source_event_key,
                    final_caption=final_caption.strip(),
                    parent=original,
                )
                candidates[selected.id] = selected
                if original is not None:
                    original.edited = True
                    original.replacement_text = final_caption.strip()
            now = datetime.now(UTC)
            displayed = exposure.displayed_at
            if displayed.tzinfo is None:
                displayed = displayed.replace(tzinfo=UTC)
            latency = max(0.0, (now - displayed).total_seconds() * 1000)
            exposure.selected_candidate_id = selected.id if selected else None
            exposure.final_caption = final_caption
            exposure.decision_type = decision_type
            exposure.decision_latency_ms = round(latency, 3)
            if selected is not None:
                selected.selected = True
                selected.decision_latency_ms = round(latency, 3)
            comparisons: list[CaptionCandidateRecord] = []
            if decision_type in {"selected", "accepted"} and selected is not None:
                comparisons = [
                    row for row in displayed_candidates.values() if row.id != selected.id
                ]
            elif decision_type == "edited" and selected is not None and original is not None:
                comparisons = [original]
            if comparisons and selected is None:
                raise RuntimeError("decision comparisons require a selected candidate")
            for other in comparisons:
                assert selected is not None
                self._persist_pair(
                    session=session,
                    proposal=proposal,
                    exposure=exposure,
                    selected=selected,
                    other=other,
                    decision_type=decision_type,
                    reason_codes=reason_codes,
                    source_event_id=source_event_id,
                    source_event_key=source_event_key,
                    policy_version=policy.version,
                )

    @staticmethod
    def _legacy_source_key(
        exposure_id: int,
        decision_type: str,
        final_caption: str | None,
        original_caption: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "exposure_id": exposure_id,
                "decision_type": decision_type,
                "final_caption": final_caption,
                "original_caption": original_caption,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"legacy-decision:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    def _human_edit_candidate(
        self,
        *,
        session: Session,
        proposal: Proposal,
        source_event_id: int | None,
        source_event_key: str,
        final_caption: str,
        parent: CaptionCandidateRecord | None,
    ) -> CaptionCandidateRecord:
        slate_id = proposal.caption_slate_id
        if slate_id is None:
            raise ValueError("human edit candidate requires a caption slate")
        derivation_key = (
            f"{source_event_key}:human-edit-candidate:"
            f"{hashlib.sha256(final_caption.encode('utf-8')).hexdigest()}"
        )
        existing = session.scalar(
            select(CaptionCandidateRecord)
            .where(CaptionCandidateRecord.derivation_key == derivation_key)
            .limit(1)
        )
        if existing is not None:
            return existing
        components = candidate_components(parent)
        slate = session.get(CaptionSlate, slate_id)
        if slate is None:
            raise ValueError("human edit candidate requires its original caption slate")
        brief = EditorialBrief.model_validate_json(slate.editorial_brief_json)
        edited_candidate = CaptionCandidate.model_validate(
            {
                "text": final_caption,
                "structure": analyze_caption(final_caption).structure,
                "language": parent.language if parent is not None else "und",
                "editorial_angle": "human_edit",
                "visible_evidence": (
                    json.loads(parent.visible_evidence_json) if parent is not None else []
                ),
                "uncertainty": (json.loads(parent.uncertainty_json) if parent is not None else []),
                "historical_evidence": (
                    json.loads(parent.historical_evidence_json) if parent is not None else []
                ),
                "feedback_evidence": (
                    json.loads(parent.feedback_evidence_json) if parent is not None else []
                ),
                "confidence": (parent.generator_confidence if parent is not None else 1.0),
            }
        )
        verification = CaptionVerifier().verify(edited_candidate, brief)
        snapshot_json, snapshot_hash = snapshot_json_and_hash(
            final_caption,
            components,
            context={
                "origin": "human_edit",
                "parent_candidate_id": parent.id if parent is not None else None,
                "source_event_key": source_event_key,
            },
        )
        maximum_rank = int(
            session.scalar(
                select(func.max(CaptionCandidateRecord.rank)).where(
                    CaptionCandidateRecord.caption_slate_id == slate_id
                )
            )
            or 0
        )
        candidate = CaptionCandidateRecord(
            channel_id=proposal.channel_id,
            caption_slate_id=slate_id,
            text=final_caption,
            language=parent.language if parent is not None else "und",
            structure=edited_candidate.structure,
            editorial_angle="human_edit",
            visible_evidence_json=(parent.visible_evidence_json if parent is not None else "[]"),
            uncertainty_json=(parent.uncertainty_json if parent is not None else "[]"),
            prohibited_claim_checks_json=json.dumps(
                verification.checks,
                sort_keys=True,
            ),
            historical_evidence_json=(
                parent.historical_evidence_json if parent is not None else "[]"
            ),
            feedback_evidence_json=(parent.feedback_evidence_json if parent is not None else "[]"),
            generator_confidence=(parent.generator_confidence if parent is not None else 1.0),
            verifier_result_json=verification.model_dump_json(),
            eligible=verification.passed,
            exclusion_reasons_json=json.dumps(
                verification.unsupported_claims,
                sort_keys=True,
            ),
            attempt_number=parent.attempt_number if parent is not None else 0,
            generation_index=maximum_rank + 1,
            grounding_score=verification.grounding_score,
            policy_score=verification.policy_score,
            style_score=components.get("style", 0.5),
            novelty_score=components.get("novelty", 0.5),
            rotation_score=components.get("rotation", 0.5),
            positive_feedback_score=components.get("positive_feedback", 0.0),
            negative_feedback_risk=components.get(
                "negative_feedback_risk",
                0.0,
            ),
            pairing_score=components.get("pairing", 0.5),
            preference_score=parent.preference_score if parent is not None else 0.5,
            final_score=parent.final_score if parent is not None else 0.5,
            rank=maximum_rank + 1,
            displayed=False,
            selected=True,
            edited=True,
            replacement_text=None,
            origin="human_edit",
            parent_candidate_id=parent.id if parent is not None else None,
            created_by="creator",
            source_proposal_event_id=source_event_id,
            derivation_key=derivation_key,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            feature_snapshot_json=snapshot_json,
            feature_snapshot_hash=snapshot_hash,
            taxonomy_version=TAXONOMY_VERSION,
            verifier_version=VERIFIER_VERSION,
        )
        session.add(candidate)
        session.flush()
        raw_provider, resolution = self.representations.resolve(
            proposal.channel_id,
            modality="text",
        )
        provider = cast(TextEmbeddingProvider, raw_provider)
        representation = RepresentationStore.persist_in_session(
            session,
            channel_id=proposal.channel_id,
            entity_type="caption_candidate",
            entity_id=candidate.id,
            field="text",
            modality="text",
            result=provider.embed_text(
                final_caption,
                purpose="caption_candidate_semantics",
            ),
            source_content_hash=content_hash(final_caption),
            metadata={
                "caption_slate_id": slate_id,
                "origin": "human_edit",
                "source_event_key": source_event_key,
                "representation_resolution": resolution.as_dict(),
            },
        )
        candidate.representation_record_id = representation.id
        return candidate

    @staticmethod
    def _persist_pair(
        *,
        session: Session,
        proposal: Proposal,
        exposure: CaptionExposure,
        selected: CaptionCandidateRecord,
        other: CaptionCandidateRecord,
        decision_type: str,
        reason_codes: list[str] | None,
        source_event_id: int | None,
        source_event_key: str,
        policy_version: str,
    ) -> None:
        slate_id = proposal.caption_slate_id
        if slate_id is None:
            raise ValueError("pairwise preference requires a caption slate")
        idempotency_key = f"caption-pair:v1:{proposal.id}:{slate_id}:{selected.id}:{other.id}"
        existing = session.scalar(
            select(PairwisePreference.id)
            .where(PairwisePreference.idempotency_key == idempotency_key)
            .limit(1)
        )
        if existing is not None:
            return
        preferred_json, preferred_hash = CaptionExposureService._ensure_snapshot(
            selected,
            context={"decision_source": source_event_key},
        )
        dispreferred_json, dispreferred_hash = CaptionExposureService._ensure_snapshot(
            other,
            context={"decision_source": source_event_key},
        )
        slate = session.get(CaptionSlate, slate_id)
        retrieval = (
            session.get(IntelligenceRetrievalRun, slate.retrieval_run_id)
            if slate is not None and slate.retrieval_run_id is not None
            else None
        )
        context_snapshot = {
            "proposal_id": proposal.id,
            "caption_slate_id": slate_id,
            "caption_exposure_id": exposure.id,
            "candidate_image_id": proposal.candidate_image_id,
            "interface_version": exposure.interface_version,
            "policy_version": policy_version,
            "style_profile_version": (retrieval.profile_version if retrieval is not None else None),
            "representation_sets": (
                json.loads(retrieval.representation_sets_json) if retrieval is not None else {}
            ),
            "retrieval_configuration_hash": (
                retrieval.configuration_hash if retrieval is not None else None
            ),
        }
        context_json = json.dumps(
            context_snapshot,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_hash = hashlib.sha256(
            (preferred_hash + dispreferred_hash + context_json + FEATURE_SCHEMA_VERSION).encode(
                "utf-8"
            )
        ).hexdigest()
        session.add(
            PairwisePreference(
                channel_id=proposal.channel_id,
                proposal_id=proposal.id,
                candidate_image_id=proposal.candidate_image_id,
                preferred_candidate_id=selected.id,
                preferred_text=selected.text,
                dispreferred_candidate_id=other.id,
                dispreferred_text=other.text,
                preference_source=("human_edit" if decision_type == "edited" else decision_type),
                label_source="human",
                strength=1.25 if decision_type == "edited" else 1.0,
                reason_codes_json=json.dumps(
                    sorted(reason_codes or (["human_edit"] if decision_type == "edited" else []))
                ),
                policy_version=policy_version,
                target="caption",
                source_proposal_event_id=source_event_id,
                source_exposure_id=exposure.id,
                source_event_key=source_event_key,
                derivation_version="decision-derivation-v1",
                idempotency_key=idempotency_key,
                preferred_features_json=preferred_json,
                dispreferred_features_json=dispreferred_json,
                context_snapshot_json=context_json,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                feature_snapshot_hash=snapshot_hash,
                group_key=(f"proposal:{proposal.id}:image:{proposal.candidate_image_id}"),
                taxonomy_version=TAXONOMY_VERSION,
                verifier_version=VERIFIER_VERSION,
                style_profile_version=(
                    retrieval.profile_version if retrieval is not None else None
                ),
                representation_sets_json=(
                    retrieval.representation_sets_json if retrieval is not None else "{}"
                ),
                retrieval_configuration_hash=(
                    retrieval.configuration_hash if retrieval is not None else None
                ),
                ranker_configuration_hash=configuration_hash(
                    {
                        "feature_schema_version": FEATURE_SCHEMA_VERSION,
                        "algorithm": "pairwise_logistic",
                    }
                ),
            )
        )

    @staticmethod
    def _ensure_snapshot(
        candidate: CaptionCandidateRecord,
        *,
        context: dict[str, object],
    ) -> tuple[str, str]:
        if (
            candidate.feature_schema_version == FEATURE_SCHEMA_VERSION
            and candidate.feature_snapshot_json
            and candidate.feature_snapshot_hash
        ):
            return (
                candidate.feature_snapshot_json,
                candidate.feature_snapshot_hash,
            )
        snapshot_json, snapshot_hash = snapshot_json_and_hash(
            candidate.text,
            candidate_components(candidate),
            context=context,
        )
        candidate.feature_schema_version = FEATURE_SCHEMA_VERSION
        candidate.feature_snapshot_json = snapshot_json
        candidate.feature_snapshot_hash = snapshot_hash
        candidate.taxonomy_version = TAXONOMY_VERSION
        candidate.verifier_version = VERIFIER_VERSION
        return snapshot_json, snapshot_hash
