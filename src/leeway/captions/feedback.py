from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from sqlalchemy import desc, select

from leeway.analysis.features import text_similarity
from leeway.db.base import Database
from leeway.db.models import CandidateImage, CaptionFeedback, Proposal
from leeway.db.repositories import audit

POSITIVE_VERDICTS = {"accepted", "edited", "preferred", "selected"}
NEGATIVE_VERDICTS = {"rejected"}
ALLOWED_VERDICTS = POSITIVE_VERDICTS | NEGATIVE_VERDICTS
ALLOWED_STRUCTURES = {"open_question", "observation", "reaction"}
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
    "human_edit",
    "selected_alternative",
    "approved",
}
OPEN_QUESTION_RE = re.compile(r"^\s*(why|how|what|who|where|when)\b", re.IGNORECASE)


def caption_structure(caption: str) -> str:
    if caption.rstrip().endswith("?") and OPEN_QUESTION_RE.match(caption):
        return "open_question"
    if caption.rstrip().endswith("?"):
        return "open_question"
    lowered = caption.lower()
    if any(
        marker in lowered
        for marker in (
            "that look",
            "the face of",
            "this should",
            "moments before",
            "when you",
            "me when",
        )
    ):
        return "reaction"
    return "observation"


class CaptionFeedbackService:
    def __init__(self, database: Database):
        self.database = database

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
        clean_note = note.strip() if note else None
        if clean_note and len(clean_note) > 1000:
            raise ValueError("feedback note must be 1000 characters or fewer")

        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
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
                    },
                )
            feedback_id = existing.id
        return self.detail(feedback_id)

    def detail(self, feedback_id: int) -> dict[str, object]:
        with self.database.session() as session:
            row = session.get(CaptionFeedback, feedback_id)
            if row is None:
                raise LookupError(f"caption feedback {feedback_id} not found")
            return self._row(row)

    def list_for_proposal(self, proposal_id: int) -> list[dict[str, object]]:
        with self.database.session() as session:
            rows = session.scalars(
                select(CaptionFeedback)
                .where(CaptionFeedback.proposal_id == proposal_id)
                .order_by(desc(CaptionFeedback.created_at), desc(CaptionFeedback.id))
            ).all()
            return [self._row(row) for row in rows]

    def context_for_candidate(self, candidate_id: int) -> dict[str, object]:
        with self.database.session() as session:
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            current_topic = self._topic(candidate)
            rows = session.scalars(
                select(CaptionFeedback)
                .order_by(desc(CaptionFeedback.created_at), desc(CaptionFeedback.id))
                .limit(200)
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
                score += min(row.id / 1_000_000, 0.05)
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

            return {
                "editorial_policy": {
                    "primary_goal": "open-ended questions that invite community discussion",
                    "recommended_structure": "open_question",
                    "required_option_mix": {
                        "open_question": 1,
                        "observation": 1,
                        "reaction": 1,
                    },
                    "avoid_yes_no_questions": True,
                    "avoid_generic_engagement_bait": True,
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
            }

    def positive_similarity(self, caption: str, context: dict[str, object]) -> float:
        examples = context.get("positive_examples", [])
        if not isinstance(examples, list):
            return 0.0
        values = [
            text_similarity(caption, str(item.get("preferred_caption") or ""))
            for item in examples
            if isinstance(item, dict) and item.get("preferred_caption")
        ]
        return max(values, default=0.0)

    def negative_similarity(self, caption: str, context: dict[str, object]) -> float:
        examples = context.get("negative_examples", [])
        if not isinstance(examples, list):
            return 0.0
        values = [
            text_similarity(caption, str(item.get("caption") or ""))
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
                score += 0.25 * max(text_similarity(first, second), 0.0)
        return score

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
