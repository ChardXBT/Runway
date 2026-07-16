from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from leeway.analysis.schemas import (
    CandidateAnalysis,
    CaptionOptions,
    HistoricalAnnotation,
    SearchPlan,
    SearchQueryFamily,
    StyleSummary,
)
from leeway.config import Settings


class AgentRuntime(Protocol):
    provider: str
    model_name: str
    last_token_usage: dict[str, object]

    async def annotate_historical_post(
        self, payload: Mapping[str, Any]
    ) -> HistoricalAnnotation: ...

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary: ...

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan: ...

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis: ...

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionOptions: ...


class MockAgentRuntime:
    provider = "mock"
    model_name = "deterministic-fixture-v1"
    last_token_usage: dict[str, object] = {}

    async def annotate_historical_post(self, payload: Mapping[str, Any]) -> HistoricalAnnotation:
        post_id = int(payload.get("post_id", 0))
        caption = str(payload.get("caption") or "")
        families = ["Synthetic Ensemble", "Synthetic Adventure", "Synthetic Comedy"]
        structures = ["observational statement", "short question", "reaction punchline"]
        intent = "discussion" if "?" in caption else "reaction"
        return HistoricalAnnotation(
            franchise=families[post_id % len(families)],
            show_name=families[post_id % len(families)],
            visible_characters=[f"Fixture character {(post_id % 5) + 1}"],
            visible_character_count=1,
            scene_description=f"Synthetic character-focused scene for catalogue post {post_id}.",
            visual_medium="digital animation still",
            composition="centered reaction" if post_id % 2 else "two-subject comparison",
            facial_emotional_cues="heightened surprise" if post_id % 2 else "confident calm",
            text_overlay=False,
            reaction_potential="high",
            caption_intent=intent,
            caption_structure=structures[post_id % len(structures)],
            humor_style="situational understatement",
            tone="playful",
            confidence={
                "franchise": 0.65,
                "characters": 0.7,
                "scene": 0.92,
                "caption": 0.95,
            },
        )

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary:
        post_ids = [int(value) for value in payload.get("representative_post_ids", [])]
        median = payload.get("median_caption_words", "unknown")
        return StyleSummary(
            summary=(
                "Fixture Qlob style favors compact, playful reaction captions "
                f"near {median} words, "
                "with questions used selectively and images framed around one clear emotional beat."
            ),
            cited_post_ids=post_ids[:5],
            rotation_observations=[
                "Alternate centered reactions with comparison compositions.",
                "Avoid repeating the same fixture franchise on consecutive days.",
            ],
        )

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan:
        underused = [str(value) for value in payload.get("underused_franchises", [])]
        topic = underused[0] if underused else "animated ensemble"
        return SearchPlan(
            query_families=[
                SearchQueryFamily(
                    purpose="reaction frames",
                    queries=[f"{topic} expressive reaction scene", f"{topic} dramatic close up"],
                ),
                SearchQueryFamily(
                    purpose="discussion prompts",
                    queries=[f"{topic} character comparison", f"{topic} team decision scene"],
                ),
                SearchQueryFamily(
                    purpose="visual variety",
                    queries=[f"{topic} wide composition", f"{topic} colorful cinematic still"],
                ),
            ],
            desired_visual_traits=["clear subject", "strong expression", "minimal overlay text"],
            excluded_concepts=[str(value) for value in payload.get("recent_exclusions", [])],
        )

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        candidate_id = int(payload.get("candidate_id", 0))
        return CandidateAnalysis(
            franchise=["Synthetic Ensemble", "Synthetic Adventure", "Synthetic Comedy"][
                candidate_id % 3
            ],
            characters=[f"Fixture candidate {(candidate_id % 7) + 1}"],
            scene_archetype="reaction" if candidate_id % 2 else "group decision",
            composition="centered" if candidate_id % 2 else "balanced two-subject",
            emotion="surprise" if candidate_id % 3 else "determination",
            text_overlay=False,
            watermark_probability=0.02,
            unsafe_probability=0.0,
            caption_potential=0.78 + (candidate_id % 5) * 0.03,
            confidence=0.9,
        )

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionOptions:
        candidate_id = int(payload.get("candidate_id", 0))
        references = [int(value) for value in payload.get("historical_post_ids", [])][:4]
        variants = [
            "That confidence lasted exactly three seconds.",
            "Would you trust this plan?",
            "Everyone saw that coming except him.",
            "A completely normal amount of dramatic tension.",
            "The face of someone who learned nothing.",
        ]
        recommended = variants[candidate_id % len(variants)]
        alternatives = [
            variants[(candidate_id + 1) % len(variants)],
            variants[(candidate_id + 2) % len(variants)],
        ]
        return CaptionOptions(
            recommended=recommended,
            alternatives=alternatives,
            rationale=(
                "Matches fixture caption length, playful tone, and reaction-led construction."
            ),
            confidence=0.86,
            referenced_historical_post_ids=references,
            factual_uncertainty_warning=None,
        )


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class OpenAIAgentRuntime:
    """Configuration-gated Responses API runtime with strict Pydantic parsing."""

    provider = "openai"

    def __init__(self, settings: Settings):
        if not settings.openai_api_key or not settings.openai_model:
            raise ValueError("OpenAI runtime requires OPENAI_API_KEY and OPENAI_MODEL")
        self.model_name = settings.openai_model
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.prompts = Path(__file__).resolve().parent / "prompts"
        self.last_token_usage: dict[str, object] = {}

    async def _parse(
        self,
        schema: type[SchemaT],
        prompt_name: str,
        payload: Mapping[str, Any],
    ) -> SchemaT:
        instructions = (self.prompts / prompt_name).read_text(encoding="utf-8")

        def request() -> SchemaT:
            response = self.client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                text_format=schema,
            )
            if response.output_parsed is None:
                raise ValueError("model returned no parsed structured output")
            usage = getattr(response, "usage", None)
            self.last_token_usage = usage.model_dump() if usage is not None else {}
            return response.output_parsed

        return await asyncio.to_thread(request)

    async def annotate_historical_post(self, payload: Mapping[str, Any]) -> HistoricalAnnotation:
        return await self._parse(HistoricalAnnotation, "annotate-history-v1.txt", payload)

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary:
        return await self._parse(StyleSummary, "style-summary-v1.txt", payload)

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan:
        return await self._parse(SearchPlan, "search-plan-v1.txt", payload)

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        return await self._parse(CandidateAnalysis, "candidate-analysis-v1.txt", payload)

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionOptions:
        return await self._parse(CaptionOptions, "captions-v1.txt", payload)


def runtime_for(settings: Settings) -> AgentRuntime:
    if settings.agent_runtime == "openai":
        return OpenAIAgentRuntime(settings)
    return MockAgentRuntime()
