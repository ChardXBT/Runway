from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from leeway.analysis.features import text_similarity
from leeway.analysis.runtime import AgentRuntime, runtime_for
from leeway.analysis.schemas import CaptionOptions
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import CandidateImage, ModelRun, Post, Proposal, utcnow
from leeway.db.repositories import audit
from leeway.intelligence.retrieval import RetrievalService


class CaptionService:
    prompt_version = "captions-v1"

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

    async def generate(self, candidate_id: int) -> CaptionOptions:
        with self.database.session() as session:
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            if candidate.hard_rejection_reason:
                raise ValueError("captions cannot be generated for a hard-rejected candidate")
            media_asset_id = candidate.media_asset_id
            analysis = json.loads(candidate.detected_topic_json)
        context = self.retrieval.context_for_candidate(media_asset_id)
        caption_examples = cast(list[dict[str, Any]], context["caption_style_examples"])
        historical_ids = [int(item["post_id"]) for item in caption_examples]
        payload: dict[str, object] = {
            "candidate_id": candidate_id,
            "candidate_analysis": analysis,
            "historical_post_ids": historical_ids,
            "retrieval_context": context,
        }
        started = utcnow()
        generated = await self.runtime.generate_caption_options(payload)
        validated = self._validated_options(candidate_id, generated)
        with self.database.session() as session:
            session.add(
                ModelRun(
                    task_type="generate_caption_options",
                    provider=self.runtime.provider,
                    model=self.runtime.model_name,
                    prompt_version=self.prompt_version,
                    input_record_ids_json=json.dumps([candidate_id, *historical_ids]),
                    request_summary_json=json.dumps(payload, sort_keys=True, default=str),
                    structured_output_json=validated.model_dump_json(),
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
                },
            )
        return validated

    def _validated_options(self, candidate_id: int, generated: CaptionOptions) -> CaptionOptions:
        with self.database.session() as session:
            existing = [
                caption for caption in session.scalars(select(Post.caption)).all() if caption
            ]
            existing.extend(
                caption
                for caption in session.scalars(select(Proposal.final_caption)).all()
                if caption
            )
        accepted: list[str] = []
        for caption in [generated.recommended, *generated.alternatives]:
            if not self._is_duplicate(caption, existing + accepted):
                accepted.append(caption.strip())
        subjects = [
            "That confidence",
            "This dramatic entrance",
            "The backup plan",
            "That suspicious silence",
            "This team meeting",
            "The heroic pose",
            "That perfect timing",
            "The last-minute idea",
            "This level of commitment",
            "The look on everyone’s face",
        ]
        endings = [
            "lasted longer than expected.",
            "needed a backup plan.",
            "answered absolutely no questions.",
            "was doing a lot of work.",
            "changed the entire mood.",
            "made the group chat go quiet.",
            "could not have arrived sooner.",
            "was somehow the calm option.",
        ]
        fallbacks = [f"{subject} {ending}" for subject in subjects for ending in endings]
        start = candidate_id % len(fallbacks)
        for offset in range(len(fallbacks) * 2):
            fallback = fallbacks[(start + offset) % len(fallbacks)]
            if len(accepted) >= 3:
                break
            if not self._is_duplicate(fallback, existing + accepted):
                accepted.append(fallback)
        if len(accepted) < 3:
            raise ValueError("could not produce three nonduplicate caption options")
        return CaptionOptions(
            recommended=accepted[0],
            alternatives=accepted[1:3],
            rationale=generated.rationale,
            confidence=generated.confidence,
            referenced_historical_post_ids=generated.referenced_historical_post_ids,
            factual_uncertainty_warning=generated.factual_uncertainty_warning,
        )

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
