from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from runway.captions.taxonomy import analyze_caption
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    CaptionFeedback,
    FeedbackSignal,
    Proposal,
    ProposalEvent,
    SearchRun,
)
from runway.db.repositories import audit, get_channel
from runway.intelligence.embeddings import (
    DeterministicTextEmbeddingProvider,
    cosine,
)
from runway.intelligence.policies import ChannelPolicyService

POSITIVE_VERDICTS = {"accepted", "edited", "preferred", "selected"}
NEGATIVE_VERDICTS = {"rejected"}
ALLOWED_VERDICTS = POSITIVE_VERDICTS | NEGATIVE_VERDICTS
ALLOWED_STRUCTURES = {
    "open_question",
    "yes_no_question",
    "observation",
    "reaction",
    "comparison",
    "prediction",
    "fill_in_blank",
    "poll",
    "quiz",
    "call_to_action",
    "explanation",
    "promotional_statement",
    "quote_or_reference",
}
ALLOWED_IMAGE_VERDICTS = {"good", "bad", "unsure"}
ALLOWED_REASON_CODES = {
    "too_generic",
    "prefer_open_question",
    "wrong_emotion",
    "wrong_character",
    "invented_context",
    "not_engaging",
    "not_funny",
    "too_long",
    "too_similar",
    "image_good_caption_bad",
    "caption_good_image_bad",
    "source_concern",
    "image_not_a_fit",
    "human_edit",
    "human_lineup_edit",
    "selected_alternative",
    "approved",
}


def caption_structure(caption: str) -> str:
    return analyze_caption(caption).structure


class CaptionFeedbackService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings
        self.text_provider = DeterministicTextEmbeddingProvider()

    def record(
        self,
        proposal_id: int,
        *,
        verdict: str,
        generated_caption: str | None = None,
        preferred_caption: str | None = None,
        preferred_structure: str | None = None,
        reason_codes: list[str] | None = None,
        image_verdict: str | None = None,
        pairing_verdict: str | None = None,
        note: str | None = None,
        source_event_id: int | None = None,
    ) -> dict[str, object]:
        normalized_verdict = verdict.strip().lower()
        if normalized_verdict not in ALLOWED_VERDICTS:
            raise ValueError(f"unsupported caption verdict: {verdict}")
        normalized_reasons = sorted(
            {value.strip() for value in reason_codes or [] if value.strip()}
        )
        unexpected = set(normalized_reasons) - ALLOWED_REASON_CODES
        if unexpected:
            raise ValueError(f"unsupported feedback reasons: {', '.join(sorted(unexpected))}")
        if image_verdict is not None and image_verdict not in ALLOWED_IMAGE_VERDICTS:
            raise ValueError(f"unsupported image verdict: {image_verdict}")
        if pairing_verdict is not None and pairing_verdict not in ALLOWED_IMAGE_VERDICTS:
            raise ValueError(f"unsupported pairing verdict: {pairing_verdict}")
        clean_note = note.strip() if note else None
        if clean_note and len(clean_note) > 1000:
            raise ValueError("feedback note must be 1000 characters or fewer")

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
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            source = (generated_caption or proposal.final_caption).strip()
            preferred = preferred_caption.strip() if preferred_caption else None
            if not source:
                raise ValueError("generated caption cannot be empty")
            if normalized_verdict in POSITIVE_VERDICTS and not preferred:
                preferred = proposal.final_caption.strip()
            structure = preferred_structure or (caption_structure(preferred) if preferred else None)
            if structure is not None and structure not in ALLOWED_STRUCTURES:
                raise ValueError(f"unsupported caption structure: {structure}")

            reasons_json = json.dumps(normalized_reasons)
            existing = session.scalar(
                select(CaptionFeedback)
                .where(
                    CaptionFeedback.proposal_id == proposal.id,
                    CaptionFeedback.verdict == normalized_verdict,
                    CaptionFeedback.generated_caption == source,
                    CaptionFeedback.preferred_caption == preferred,
                    CaptionFeedback.reason_codes_json == reasons_json,
                    CaptionFeedback.image_verdict == image_verdict,
                    CaptionFeedback.note == clean_note,
                )
                .order_by(desc(CaptionFeedback.id))
                .limit(1)
            )
            if existing is None:
                existing = CaptionFeedback(
                    proposal_id=proposal.id,
                    candidate_image_id=proposal.candidate_image_id,
                    verdict=normalized_verdict,
                    generated_caption=source,
                    preferred_caption=preferred,
                    preferred_structure=structure,
                    reason_codes_json=reasons_json,
                    image_verdict=image_verdict,
                    note=clean_note,
                )
                session.add(existing)
                session.flush()
                audit(
                    session,
                    "caption_feedback_recorded",
                    "proposal",
                    proposal.id,
                    {
                        "feedback_id": existing.id,
                        "verdict": normalized_verdict,
                        "reason_codes": normalized_reasons,
                        "preferred_structure": structure,
                        "image_verdict": image_verdict,
                        "pairing_verdict": pairing_verdict,
                    },
                )
            self._ensure_normalized_signals(
                session=session,
                feedback=existing,
                proposal=proposal,
                policy_version=policy.version,
                pairing_verdict=pairing_verdict,
                source_event_id=source_event_id,
            )
            feedback_id = existing.id
        return self.detail(feedback_id)

    def reconcile_legacy(self) -> dict[str, int]:
        """Idempotently enrich or derive canonical signals from legacy rows."""
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            channel_id = channel.id
        policy = ChannelPolicyService(
            self.database,
            self.settings,
        ).ensure_defaults(channel_id)
        created = 0
        enriched = 0
        with self.database.session() as session:
            rows = session.scalars(
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(Proposal.channel_id == channel_id)
                .order_by(CaptionFeedback.id)
            ).all()
            for feedback in rows:
                proposal = session.get(Proposal, feedback.proposal_id)
                if proposal is None:
                    continue
                result = self._ensure_normalized_signals(
                    session=session,
                    feedback=feedback,
                    proposal=proposal,
                    policy_version=policy.version,
                    pairing_verdict=None,
                    source_event_id=None,
                )
                created += result["created"]
                enriched += result["enriched"]
            session.flush()
            canonical_signals = int(
                session.scalar(
                    select(func.count(FeedbackSignal.id)).where(
                        FeedbackSignal.channel_id == channel_id
                    )
                )
                or 0
            )
        return {
            "legacy_rows": len(rows),
            "created": created,
            "enriched": enriched,
            "canonical_signals": canonical_signals,
        }

    def _ensure_normalized_signals(
        self,
        *,
        session: Session,
        feedback: CaptionFeedback,
        proposal: Proposal,
        policy_version: str,
        pairing_verdict: str | None,
        source_event_id: int | None,
    ) -> dict[str, int]:
        if source_event_id is not None:
            source_event = session.get(ProposalEvent, source_event_id)
            if source_event is None or source_event.proposal_id != proposal.id:
                raise ValueError("feedback source event does not belong to the proposal")
        source_event_key = (
            f"proposal-event:{source_event_id}"
            if source_event_id is not None
            else f"caption-feedback:{feedback.id}"
        )
        inferred_pairing = pairing_verdict
        if inferred_pairing is None and feedback.image_verdict is not None:
            inferred_pairing = (
                "good"
                if feedback.image_verdict == "good" and feedback.verdict in POSITIVE_VERDICTS
                else (
                    "bad"
                    if feedback.image_verdict == "bad" or feedback.verdict in NEGATIVE_VERDICTS
                    else "unsure"
                )
            )
        specifications: list[tuple[str, str, str | None]] = [
            (
                "caption",
                feedback.verdict,
                feedback.preferred_caption or feedback.generated_caption,
            )
        ]
        if feedback.image_verdict is not None:
            specifications.append(
                (
                    "image",
                    self._normalized_target_verdict(feedback.image_verdict),
                    None,
                )
            )
        if inferred_pairing is not None:
            specifications.append(
                (
                    "pairing",
                    self._normalized_target_verdict(inferred_pairing),
                    feedback.preferred_caption or feedback.generated_caption,
                )
            )

        created = 0
        enriched = 0
        for target, verdict, value_text in specifications:
            idempotency_key = f"feedback-normalization-v1:{feedback.id}:{target}"
            signal = session.scalar(
                select(FeedbackSignal)
                .where(FeedbackSignal.idempotency_key == idempotency_key)
                .limit(1)
            )
            if signal is None:
                signal = session.scalar(
                    select(FeedbackSignal)
                    .where(
                        FeedbackSignal.channel_id == proposal.channel_id,
                        FeedbackSignal.proposal_id == proposal.id,
                        FeedbackSignal.candidate_image_id == proposal.candidate_image_id,
                        FeedbackSignal.target == target,
                        FeedbackSignal.verdict == verdict,
                        FeedbackSignal.value_text == value_text,
                        FeedbackSignal.reason_codes_json == feedback.reason_codes_json,
                        FeedbackSignal.note == feedback.note,
                        FeedbackSignal.source_caption_feedback_id.is_(None),
                    )
                    .order_by(FeedbackSignal.id)
                    .limit(1)
                )
                if signal is not None:
                    signal.source_caption_feedback_id = feedback.id
                    signal.source_proposal_event_id = source_event_id
                    signal.source_event_key = source_event_key
                    signal.derivation_version = "feedback-normalization-v1"
                    signal.idempotency_key = idempotency_key
                    enriched += 1
                    continue
            if signal is not None:
                continue
            session.add(
                FeedbackSignal(
                    channel_id=proposal.channel_id,
                    proposal_id=proposal.id,
                    candidate_image_id=proposal.candidate_image_id,
                    target=target,
                    verdict=verdict,
                    value_text=value_text,
                    reason_codes_json=feedback.reason_codes_json,
                    note=feedback.note,
                    source="creator",
                    policy_version=policy_version,
                    source_proposal_event_id=source_event_id,
                    source_caption_feedback_id=feedback.id,
                    source_event_key=source_event_key,
                    derivation_version="feedback-normalization-v1",
                    idempotency_key=idempotency_key,
                    created_at=feedback.created_at,
                )
            )
            created += 1
        return {"created": created, "enriched": enriched}

    def detail(self, feedback_id: int) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            row = session.scalar(
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(
                    CaptionFeedback.id == feedback_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if row is None:
                raise LookupError(f"caption feedback {feedback_id} not found")
            return self._row(row)

    def list_for_proposal(self, proposal_id: int) -> list[dict[str, object]]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            rows = session.scalars(
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(
                    CaptionFeedback.proposal_id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
                .order_by(desc(CaptionFeedback.created_at), desc(CaptionFeedback.id))
            ).all()
            return [self._row(row) for row in rows]

    def context_for_candidate(self, candidate_id: int) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            search_run = session.get(SearchRun, candidate.search_run_id)
            if search_run is None or search_run.channel_id != channel.id:
                raise LookupError(f"candidate {candidate_id} has no search run")
            channel_id = search_run.channel_id
            current_topic = self._topic(candidate)
            rows = session.scalars(
                select(FeedbackSignal)
                .where(
                    FeedbackSignal.channel_id == channel_id,
                    FeedbackSignal.target == "caption",
                )
                .order_by(desc(FeedbackSignal.created_at), desc(FeedbackSignal.id))
                .limit(200)
            ).all()
            image_signals = session.scalars(
                select(FeedbackSignal)
                .where(
                    FeedbackSignal.channel_id == channel_id,
                    FeedbackSignal.target == "image",
                )
                .order_by(desc(FeedbackSignal.created_at), desc(FeedbackSignal.id))
                .limit(100)
            ).all()
            feedback_candidates = {
                item.id: item
                for item in session.scalars(
                    select(CandidateImage).where(
                        CandidateImage.id.in_(
                            {
                                row.candidate_image_id
                                for row in rows
                                if row.candidate_image_id is not None
                            }
                        )
                    )
                ).all()
            }

            scored: list[tuple[float, int, FeedbackSignal]] = []
            for row in rows:
                other = (
                    feedback_candidates.get(row.candidate_image_id)
                    if row.candidate_image_id is not None
                    else None
                )
                score = self._topic_relevance(current_topic, self._topic(other) if other else {})
                structure = caption_structure(row.value_text) if row.value_text else None
                score += 0.2 if structure == "open_question" else 0.0
                created = row.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                age_days = max(
                    0.0,
                    (datetime.now(UTC) - created).total_seconds() / 86400,
                )
                score += 0.15 * math.exp(-math.log(2) * age_days / 90.0)
                scored.append((score, row.id, row))

            positive_rows = [
                row
                for _score, _row_id, row in sorted(scored, reverse=True)
                if row.verdict in POSITIVE_VERDICTS and row.value_text
            ][:8]
            negative_rows = [
                row
                for _score, _row_id, row in sorted(scored, reverse=True)
                if row.verdict in NEGATIVE_VERDICTS
            ][:6]
            all_positive = [row for row in rows if row.verdict in POSITIVE_VERDICTS]
            structure_counts = Counter(
                caption_structure(row.value_text) for row in all_positive if row.value_text
            )
            reason_counts: Counter[str] = Counter()
            for row in rows:
                reason_counts.update(json.loads(row.reason_codes_json))

            policy = ChannelPolicyService(
                self.database,
                self.settings,
            ).ensure_defaults(channel_id)
            return {
                "editorial_policy": {
                    "preferred_structures": policy.preferred_structures,
                    "recommended_structure": policy.preferred_structures[0],
                    "question_first": policy.question_first,
                    "language": policy.language,
                    "locale": policy.locale,
                    "avoid_generic_engagement_bait": True,
                    "policy_version": policy.version,
                },
                "positive_examples": [
                    {
                        "feedback_signal_id": row.id,
                        "generated_caption": row.value_text,
                        "preferred_caption": row.value_text,
                        "preferred_structure": (
                            caption_structure(row.value_text) if row.value_text else None
                        ),
                        "reason_codes": json.loads(row.reason_codes_json),
                        "source_event_key": row.source_event_key,
                    }
                    for row in positive_rows
                ],
                "negative_examples": [
                    {
                        "feedback_signal_id": row.id,
                        "caption": row.value_text,
                        "reason_codes": json.loads(row.reason_codes_json),
                        "note": row.note,
                        "source_event_key": row.source_event_key,
                    }
                    for row in negative_rows
                ],
                "learned_preferences": {
                    "positive_feedback_count": len(all_positive),
                    "preferred_structures": structure_counts.most_common(),
                    "common_feedback_reasons": reason_counts.most_common(10),
                },
                "image_preferences": {
                    "accepted": sum(signal.verdict == "accepted" for signal in image_signals),
                    "rejected": sum(signal.verdict == "rejected" for signal in image_signals),
                    "unsure": sum(signal.verdict == "unsure" for signal in image_signals),
                },
            }

    def positive_similarity(self, caption: str, context: dict[str, object]) -> float:
        examples = context.get("positive_examples", [])
        if not isinstance(examples, list):
            return 0.0
        values = [
            self._semantic_similarity(
                caption,
                str(item.get("preferred_caption") or ""),
            )
            for item in examples
            if isinstance(item, dict) and item.get("preferred_caption")
        ]
        return max(values, default=0.0)

    def negative_similarity(self, caption: str, context: dict[str, object]) -> float:
        examples = context.get("negative_examples", [])
        if not isinstance(examples, list):
            return 0.0
        values = [
            self._semantic_similarity(caption, str(item.get("caption") or ""))
            for item in examples
            if isinstance(item, dict) and item.get("caption")
        ]
        return max(values, default=0.0)

    @staticmethod
    def _topic(candidate: CandidateImage | None) -> dict[str, Any]:
        if candidate is None:
            return {}
        try:
            value = json.loads(candidate.detected_topic_json)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _topic_relevance(current: dict[str, Any], other: dict[str, Any]) -> float:
        score = 0.0
        if current.get("franchise") and current.get("franchise") == other.get("franchise"):
            score += 1.0
        current_characters = {str(value).lower() for value in current.get("characters", [])}
        other_characters = {str(value).lower() for value in other.get("characters", [])}
        if current_characters and other_characters:
            score += 0.5 * len(current_characters & other_characters)
        for key in ("scene_archetype", "emotion", "composition"):
            first = str(current.get(key) or "")
            second = str(other.get(key) or "")
            if first and second:
                provider = DeterministicTextEmbeddingProvider()
                first_vector = provider.embed_text(first, purpose="feedback_topic").as_array()[0]
                second_vector = provider.embed_text(
                    second,
                    purpose="feedback_topic",
                ).as_array()[0]
                score += 0.25 * max(cosine(first_vector, second_vector), 0.0)
        return score

    def _semantic_similarity(self, first: str, second: str) -> float:
        first_vector = self.text_provider.embed_text(
            first,
            purpose="caption_feedback",
        ).as_array()[0]
        second_vector = self.text_provider.embed_text(
            second,
            purpose="caption_feedback",
        ).as_array()[0]
        return cosine(first_vector, second_vector)

    @staticmethod
    def _normalized_target_verdict(value: str) -> str:
        return {"good": "accepted", "bad": "rejected", "unsure": "unsure"}[value]

    @staticmethod
    def _row(row: CaptionFeedback) -> dict[str, object]:
        return {
            "id": row.id,
            "proposal_id": row.proposal_id,
            "candidate_image_id": row.candidate_image_id,
            "verdict": row.verdict,
            "generated_caption": row.generated_caption,
            "preferred_caption": row.preferred_caption,
            "preferred_structure": row.preferred_structure,
            "reason_codes": json.loads(row.reason_codes_json),
            "image_verdict": row.image_verdict,
            "note": row.note,
            "created_at": row.created_at.isoformat(),
        }
