from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from leeway.analysis.features import (
    caption_features,
    qlob_style_score,
    text_similarity,
)
from leeway.analysis.runtime import AgentRuntime, runtime_for
from leeway.analysis.schemas import CaptionCandidate, CaptionCandidateSet, CaptionOptions
from leeway.captions.feedback import CaptionFeedbackService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import CandidateImage, MediaAsset, ModelRun, Post, Proposal, utcnow
from leeway.db.repositories import audit
from leeway.intelligence.retrieval import RetrievalService

OPEN_QUESTION_RE = re.compile(r"^\s*(why|how|what|who|where|when)\b", re.IGNORECASE)
YES_NO_QUESTION_RE = re.compile(
    r"^\s*(is|are|was|were|do|does|did|can|could|would|will|has|have)\b",
    re.IGNORECASE,
)
GENERIC_PATTERNS = (
    "what do you think",
    "comment below",
    "is very ",
    "looks very ",
    "seems very ",
)


class CaptionService:
    prompt_version = "captions-v2"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)
        self.retrieval = RetrievalService(database, settings)
        self.feedback = CaptionFeedbackService(database)

    async def generate(self, candidate_id: int) -> CaptionOptions:
        with self.database.session() as session:
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            if candidate.hard_rejection_reason:
                raise ValueError("captions cannot be generated for a hard-rejected candidate")
            media_asset_id = candidate.media_asset_id
            media = session.get(MediaAsset, media_asset_id)
            if media is None:
                raise LookupError(f"candidate {candidate_id} has no media asset")
            image_path = self.settings.resolved_data_dir / media.local_path
            analysis = json.loads(candidate.detected_topic_json)
        context = self.retrieval.context_for_candidate(
            media_asset_id,
            candidate_id=candidate_id,
        )
        caption_examples = cast(list[dict[str, Any]], context["caption_style_examples"])
        historical_ids = [int(item["post_id"]) for item in caption_examples]
        payload: dict[str, object] = {
            "candidate_id": candidate_id,
            "candidate_analysis": analysis,
            "historical_post_ids": historical_ids,
            "retrieval_context": context,
            "_image_path": str(image_path),
        }
        started = utcnow()
        generated = await self.runtime.generate_caption_options(payload)
        validated, ranking = self._validated_options(
            candidate_id,
            generated,
            context,
            analysis,
            allowed_reference_ids=historical_ids,
        )
        with self.database.session() as session:
            session.add(
                ModelRun(
                    task_type="generate_caption_options",
                    provider=self.runtime.provider,
                    model=self.runtime.model_name,
                    prompt_version=self.prompt_version,
                    input_record_ids_json=json.dumps([candidate_id, *historical_ids]),
                    request_summary_json=json.dumps(payload, sort_keys=True, default=str),
                    structured_output_json=json.dumps(
                        {
                            "raw_candidates": generated.model_dump(),
                            "selected": validated.model_dump(),
                            "ranking": ranking,
                        },
                        sort_keys=True,
                    ),
                    token_usage_json=json.dumps(self.runtime.last_token_usage, sort_keys=True),
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    status="completed",
                )
            )
            audit(
                session,
                "captions_generated",
                "candidate_image",
                candidate_id,
                {
                    "prompt_version": self.prompt_version,
                    "historical_post_ids": historical_ids,
                    "recommended_structure": "open_question",
                    "candidate_count": len(generated.candidates),
                },
            )
        return validated

    def _validated_options(
        self,
        candidate_id: int,
        generated: CaptionCandidateSet,
        context: dict[str, object],
        analysis: dict[str, Any],
        *,
        allowed_reference_ids: list[int],
    ) -> tuple[CaptionOptions, list[dict[str, object]]]:
        with self.database.session() as session:
            existing = [
                caption for caption in session.scalars(select(Post.caption)).all() if caption
            ]
            existing.extend(
                caption
                for caption in session.scalars(select(Proposal.final_caption)).all()
                if caption
            )

        grounded_candidates = self._grounded_question_candidates(analysis)
        grounded_text = {item.text for item in grounded_candidates}
        pool = [*grounded_candidates, *generated.candidates]
        accepted: list[CaptionCandidate] = []
        seen: list[str] = []
        for candidate in pool:
            text = candidate.text.strip()
            if not text or self._is_duplicate(text, seen):
                continue
            seen.append(text)
            if self._is_duplicate(text, existing):
                continue
            accepted.append(
                CaptionCandidate(
                    text=text,
                    structure=self._normalized_structure(candidate),
                )
            )

        accepted.extend(self._fallback_candidates(candidate_id, accepted, existing))
        stats = cast(
            dict[str, Any],
            cast(dict[str, object], context.get("style_profile", {})).get("caption_statistics", {}),
        )
        feedback_context = cast(
            dict[str, object],
            context.get("feedback_context", {}),
        )
        historical_captions = [
            str(item.get("caption") or "")
            for item in cast(list[dict[str, Any]], context.get("caption_style_examples", []))
        ]
        ranking = [
            self._score_candidate(
                candidate,
                stats,
                feedback_context,
                existing,
                historical_captions,
                grounded_text,
            )
            for candidate in accepted
        ]
        ranking.sort(
            key=lambda item: (cast(float, item["score"]), str(item["text"])),
            reverse=True,
        )

        selected: list[dict[str, object]] = []
        for structure in ("open_question", "observation", "reaction"):
            match = next(
                (
                    row
                    for row in ranking
                    if row["structure"] == structure
                    and (
                        structure != "open_question"
                        or row["grounded"]
                        or not any(
                            item["structure"] == "open_question" and item["grounded"]
                            for item in ranking
                        )
                    )
                    and row["text"] not in {item["text"] for item in selected}
                ),
                None,
            )
            if match is not None:
                selected.append(match)
        for row in ranking:
            if len(selected) >= 3:
                break
            if row["text"] not in {item["text"] for item in selected}:
                selected.append(row)
        if len(selected) < 3 or selected[0]["structure"] != "open_question":
            raise ValueError("could not produce a question-first set of three unique captions")

        supplied_references = [
            value
            for value in generated.referenced_historical_post_ids
            if value in set(allowed_reference_ids)
        ]
        references = supplied_references or allowed_reference_ids[:4]
        recommended = str(selected[0]["text"])
        alternatives = [str(item["text"]) for item in selected[1:3]]
        rationale = (
            "Question-first ranking selected an open-ended prompt, then preserved one "
            "observational and one reaction alternative. " + generated.rationale.strip()
        )
        result = CaptionOptions(
            recommended=recommended,
            alternatives=alternatives,
            rationale=rationale,
            confidence=generated.confidence,
            referenced_historical_post_ids=references,
            factual_uncertainty_warning=generated.factual_uncertainty_warning,
        )
        return result, ranking

    def _score_candidate(
        self,
        candidate: CaptionCandidate,
        stats: dict[str, Any],
        feedback_context: dict[str, object],
        existing: list[str],
        historical_captions: list[str],
        grounded_text: set[str],
    ) -> dict[str, object]:
        text = candidate.text.strip()
        features = caption_features(text)
        word_count = int(features["word_count"])
        percentiles = cast(dict[str, float], stats.get("word_percentiles", {}))
        p25 = float(percentiles.get("p25", 3))
        p75 = float(percentiles.get("p75", 8))
        if p25 <= word_count <= p75:
            length_score = 1.0
        else:
            distance = min(abs(word_count - p25), abs(word_count - p75))
            median_raw = stats.get("median_words", 5)
            median_words = float(median_raw) if isinstance(median_raw, (int, float)) else 5.0
            length_score = math.exp(-distance / max(median_words, 1))

        is_open = bool(text.endswith("?") and OPEN_QUESTION_RE.match(text))
        is_yes_no = bool(text.endswith("?") and YES_NO_QUESTION_RE.match(text))
        structure_score = (
            1.0 if is_open else (0.35 if candidate.structure == "open_question" else 0)
        )
        positive_similarity = self.feedback.positive_similarity(text, feedback_context)
        negative_similarity = self.feedback.negative_similarity(text, feedback_context)
        comparison = [*existing, *historical_captions]
        highest_similarity = max(
            (max(text_similarity(text, other), 0.0) for other in comparison if other),
            default=0.0,
        )
        novelty_score = max(0.0, 1.0 - highest_similarity)
        generic_penalty = 0.2 if any(marker in text.lower() for marker in GENERIC_PATTERNS) else 0.0
        yes_no_penalty = 0.15 if is_yes_no else 0.0
        negative_penalty = 0.12 * max(negative_similarity, 0.0)
        grounding_bonus = 0.12 if text in grounded_text else 0.0
        score = (
            0.36 * structure_score
            + 0.26 * qlob_style_score(text, stats)
            + 0.14 * length_score
            + 0.14 * novelty_score
            + 0.10 * max(positive_similarity, 0.0)
            + grounding_bonus
            - generic_penalty
            - yes_no_penalty
            - negative_penalty
        )
        return {
            "text": text,
            "structure": candidate.structure,
            "score": round(max(0.0, min(1.0, score)), 6),
            "open_question": is_open,
            "grounded": text in grounded_text,
            "style_score": qlob_style_score(text, stats),
            "length_score": round(length_score, 6),
            "novelty_score": round(novelty_score, 6),
            "positive_feedback_similarity": round(max(positive_similarity, 0.0), 6),
            "negative_feedback_similarity": round(max(negative_similarity, 0.0), 6),
            "penalties": {
                "generic": generic_penalty,
                "yes_no_question": yes_no_penalty,
                "negative_feedback": round(negative_penalty, 6),
            },
        }

    @staticmethod
    def _normalized_structure(candidate: CaptionCandidate) -> str:
        text = candidate.text.strip()
        if text.endswith("?") and OPEN_QUESTION_RE.match(text):
            return "open_question"
        if candidate.structure == "open_question":
            return "observation"
        return candidate.structure

    @staticmethod
    def _grounded_question_candidates(
        analysis: dict[str, Any],
    ) -> list[CaptionCandidate]:
        confidence = float(analysis.get("confidence") or 0)
        characters = analysis.get("characters") or []
        emotion = str(analysis.get("emotion") or "").strip()
        if confidence < 0.8 or not characters or not emotion:
            return []
        character = CaptionService._character_for_question(str(characters[0]))
        emotion_word = CaptionService._emotion_for_question(emotion)
        if not character or not emotion_word or emotion_word in {"unknown", "uncertain"}:
            return []
        return [
            CaptionCandidate(
                text=f"Why is {character} so {emotion_word}?",
                structure="open_question",
            ),
        ]

    @staticmethod
    def _character_for_question(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9 .'-]", "", value).strip()
        parts = cleaned.split()
        if not parts:
            return ""
        if parts[0].rstrip(".").lower() in {"mr", "mrs", "ms", "dr", "professor"}:
            return " ".join(parts[:2])[:40]
        return parts[0][:40]

    @staticmethod
    def _emotion_for_question(value: str) -> str:
        lowered = re.sub(r"[^a-z ]", " ", value.lower())
        words = set(lowered.split())
        mappings = (
            ({"excited", "excitement", "thrilled", "eager"}, "excited"),
            ({"surprise", "surprised", "shocked", "astonished"}, "surprised"),
            ({"angry", "anger", "furious", "annoyed"}, "angry"),
            ({"sad", "sadness", "upset", "dejected"}, "upset"),
            ({"worried", "anxious", "nervous", "afraid", "fear"}, "worried"),
            ({"happy", "happiness", "joy", "joyful", "delighted"}, "happy"),
            ({"confused", "confusion", "puzzled", "bewildered"}, "confused"),
            ({"determined", "determination", "confident", "confidence"}, "confident"),
            ({"suspicious", "skeptical", "doubtful"}, "suspicious"),
            ({"embarrassed", "awkward", "ashamed"}, "embarrassed"),
            ({"tired", "exhausted", "sleepy"}, "tired"),
            ({"bored", "unimpressed"}, "unimpressed"),
        )
        for markers, adjective in mappings:
            if words & markers:
                return adjective
        compact = " ".join(lowered.split())
        return compact if 0 < len(compact.split()) <= 2 else ""

    def _fallback_candidates(
        self,
        candidate_id: int,
        accepted: list[CaptionCandidate],
        existing: list[str],
    ) -> list[CaptionCandidate]:
        questions = [
            "What happened right before this?",
            "How did things get to this point?",
            "Why does this feel like trouble?",
            "What has everyone so interested?",
            "How would you explain this scene?",
        ]
        observations = [
            "That confidence lasted exactly three seconds.",
            "Everyone saw that coming except him.",
            "A completely normal amount of dramatic tension.",
            "That suspicious silence says everything.",
        ]
        reactions = [
            "This should end well.",
            "The face of someone who learned nothing.",
            "Moments before the plan changed.",
            "That escalated with impressive speed.",
        ]
        rows = [
            *[CaptionCandidate(text=value, structure="open_question") for value in questions],
            *[CaptionCandidate(text=value, structure="observation") for value in observations],
            *[CaptionCandidate(text=value, structure="reaction") for value in reactions],
        ]
        start = candidate_id % len(rows)
        result: list[CaptionCandidate] = []
        current = [item.text for item in accepted]
        for offset in range(len(rows)):
            candidate = rows[(start + offset) % len(rows)]
            if self._is_duplicate(candidate.text, existing + current):
                continue
            result.append(candidate)
            current.append(candidate.text)
        return result

    def _is_duplicate(self, caption: str, existing: list[str]) -> bool:
        normalized = self._normalize(caption)
        for other in existing:
            if normalized == self._normalize(other):
                return True
            if text_similarity(caption, other) >= self.settings.caption_duplicate_threshold:
                return True
        return False

    @staticmethod
    def _normalize(caption: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", caption.lower()).strip()
