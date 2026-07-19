from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from runway.captions.taxonomy import analyze_caption
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    CaptionFeedback,
    FeedbackSignal,
    Proposal,
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
                session.add(
                    FeedbackSignal(
                        channel_id=proposal.channel_id,
                        proposal_id=proposal.id,
                        candidate_image_id=proposal.candidate_image_id,
                        target="caption",
                        verdict=normalized_verdict,
                        value_text=preferred or source,
                        reason_codes_json=reasons_json,
                        note=clean_note,
                        source="creator",
                        policy_version=policy.version,
                    )
                )
                if image_verdict is not None:
                    session.add(
                        FeedbackSignal(
                            channel_id=proposal.channel_id,
                            proposal_id=proposal.id,
                            candidate_image_id=proposal.candidate_image_id,
                            target="image",
                            verdict=self._normalized_target_verdict(image_verdict),
                            value_text=None,
                            reason_codes_json=reasons_json,
                            note=clean_note,
                            source="creator",
                            policy_version=policy.version,
                        )
                    )
                inferred_pairing = pairing_verdict
                if inferred_pairing is None and image_verdict is not None:
                    inferred_pairing = (
                        "good"
                        if image_verdict == "good"
                        and normalized_verdict in POSITIVE_VERDICTS
                        else (
                            "bad"
                            if image_verdict == "bad"
                            or normalized_verdict in NEGATIVE_VERDICTS
                            else "unsure"
                        )
                    )
                if inferred_pairing is not None:
                    session.add(
                        FeedbackSignal(
                            channel_id=proposal.channel_id,
                            proposal_id=proposal.id,
                            candidate_image_id=proposal.candidate_image_id,
                            target="pairing",
                            verdict=self._normalized_target_verdict(inferred_pairing),
                            value_text=preferred or source,
                            reason_codes_json=reasons_json,
                            note=clean_note,
                            source="creator",
                            policy_version=policy.version,
                        )
                    )
            feedback_id = existing.id
        return self.detail(feedback_id)

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
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(Proposal.channel_id == channel_id)
                .order_by(desc(CaptionFeedback.created_at), desc(CaptionFeedback.id))
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
                        CandidateImage.id.in_({row.candidate_image_id for row in rows})
                    )
                ).all()
            }

            scored: list[tuple[float, int, CaptionFeedback]] = []
            for row in rows:
                other = feedback_candidates.get(row.candidate_image_id)
                score = self._topic_relevance(current_topic, self._topic(other) if other else {})
                score += 0.2 if row.preferred_structure == "open_question" else 0.0
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
                if row.verdict in POSITIVE_VERDICTS and row.preferred_caption
            ][:8]
            negative_rows = [
                row
                for _score, _row_id, row in sorted(scored, reverse=True)
                if row.verdict in NEGATIVE_VERDICTS
            ][:6]
            all_positive = [row for row in rows if row.verdict in POSITIVE_VERDICTS]
            structure_counts = Counter(
                row.preferred_structure for row in all_positive if row.preferred_structure
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
                        "feedback_id": row.id,
                        "generated_caption": row.generated_caption,
                        "preferred_caption": row.preferred_caption,
                        "preferred_structure": row.preferred_structure,
                        "reason_codes": json.loads(row.reason_codes_json),
                    }
                    for row in positive_rows
                ],
                "negative_examples": [
                    {
                        "feedback_id": row.id,
                        "caption": row.generated_caption,
                        "reason_codes": json.loads(row.reason_codes_json),
                        "note": row.note,
                    }
                    for row in negative_rows
                ],
                "learned_preferences": {
                    "positive_feedback_count": len(all_positive),
                    "preferred_structures": structure_counts.most_common(),
                    "common_feedback_reasons": reason_counts.most_common(10),
                },
                "image_preferences": {
                    "accepted": sum(
                        signal.verdict == "accepted" for signal in image_signals
                    ),
                    "rejected": sum(
                        signal.verdict == "rejected" for signal in image_signals
                    ),
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
