from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from openai import OpenAI
from pydantic import BaseModel

from runway.analysis.schemas import (
    CandidateAnalysis,
    CaptionCandidate,
    CaptionCandidateSet,
    CaptionGroundingAssessment,
    CaptionGroundingAudit,
    HistoricalAnnotation,
    HistoricalAnnotationBatch,
    HistoricalAnnotationResult,
    SearchPlan,
    SearchQueryFamily,
    ShadowEditorialRecommendation,
    StyleSummary,
)
from runway.config import Settings


def _mock_subject_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 .'-]", "", value).strip()
    parts = cleaned.split()
    if not parts:
        return "this scene"
    generic_markers = {
        "animated",
        "attendee",
        "attendees",
        "character",
        "characters",
        "figure",
        "group",
        "people",
        "person",
        "unidentified",
        "unknown",
    }
    words = set(cleaned.casefold().replace("-", " ").split())
    if parts[0][:1].islower() or words & generic_markers:
        plural_markers = {
            "attendees",
            "characters",
            "group",
            "people",
        }
        return "they" if words & plural_markers else "the subject"
    if cleaned.casefold().startswith("fixture subject "):
        return cleaned
    return parts[0]


def _mock_emotion_label(value: str) -> str:
    words = set(re.sub(r"[^a-z ]", " ", value.casefold()).split())
    mappings = (
        ({"excited", "excitement", "thrilled", "eager"}, "excited"),
        ({"surprise", "surprised", "shocked", "astonished"}, "surprised"),
        ({"angry", "anger", "furious", "annoyed"}, "angry"),
        ({"sad", "sadness", "upset", "dejected"}, "upset"),
        ({"worried", "anxious", "nervous", "afraid", "fear"}, "worried"),
        ({"happy", "happiness", "joy", "joyful", "delighted"}, "happy"),
        ({"confused", "confusion", "puzzled", "bewildered"}, "confused"),
        ({"determination", "determined", "confident", "confidence"}, "determined"),
        ({"curiosity", "curious"}, "curious"),
        ({"concentration", "focus", "focused"}, "focused"),
        ({"anticipation", "expectant"}, "expectant"),
        ({"concern", "concerned"}, "concerned"),
        ({"delight"}, "delighted"),
    )
    for markers, label in mappings:
        if words & markers:
            return label
    compact = " ".join(sorted(words))
    return compact if 0 < len(compact.split()) <= 2 else "curious"


class AgentRuntimeError(RuntimeError):
    """A model runtime failed without allowing an implicit provider fallback."""


class AgentTerminalError(AgentRuntimeError):
    """Stop the current batch because retrying would be unsafe or waste usage."""


class AgentAuthenticationRequired(AgentTerminalError):
    """The configured runtime requires an explicit user sign-in."""


class AgentUsageLimitReached(AgentTerminalError):
    """The included ChatGPT/Codex usage limit has been reached."""


class PaidApiAuthenticationBlocked(AgentTerminalError):
    """Runway refuses to run Codex with usage-billed API authentication."""


class AgentRuntime(Protocol):
    provider: str
    model_name: str
    last_token_usage: dict[str, object]

    async def annotate_historical_post(
        self, payload: Mapping[str, Any]
    ) -> HistoricalAnnotation: ...

    async def annotate_historical_posts(
        self, payload: Mapping[str, Any]
    ) -> HistoricalAnnotationBatch: ...

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary: ...

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan: ...

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis: ...

    async def audit_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis: ...

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionCandidateSet: ...

    async def verify_caption_grounding(
        self, payload: Mapping[str, Any]
    ) -> CaptionGroundingAudit: ...

    async def simulate_editorial_decision(
        self, payload: Mapping[str, Any]
    ) -> ShadowEditorialRecommendation: ...


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
        entity_name = f"Fixture subject {(post_id % 5) + 1}"
        action = "reacting" if post_id % 2 else "comparing options"
        return HistoricalAnnotation(
            franchise=families[post_id % len(families)],
            show_name=families[post_id % len(families)],
            visible_characters=[entity_name],
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
                "entities": 0.88,
                "actions": 0.84,
                "relationships": 0.72,
                "ocr": 0.9,
            },
            entities=[
                {
                    "name": entity_name,
                    "entity_type": "fictional_character",
                    "confidence": 0.88,
                    "canonical_name": entity_name,
                }
            ],
            people=[],
            organizations=[],
            products=[],
            teams=[],
            locations=[],
            animals=[],
            objects=["fixture prop"],
            actions=[action],
            relationships=[],
            setting="synthetic scene",
            ocr_text=[],
            editorial_angle="reaction" if post_id % 2 else "comparison",
            audience_invitation_type="question" if "?" in caption else "reaction",
            image_caption_relationship="caption interprets visible reaction",
        )

    async def annotate_historical_posts(
        self, payload: Mapping[str, Any]
    ) -> HistoricalAnnotationBatch:
        results: list[HistoricalAnnotationResult] = []
        for raw_post in payload.get("posts", []):
            if not isinstance(raw_post, Mapping):
                continue
            annotation = await self.annotate_historical_post(raw_post)
            results.append(
                HistoricalAnnotationResult(
                    post_id=int(raw_post.get("post_id", 0)),
                    annotation=annotation,
                )
            )
        return HistoricalAnnotationBatch(annotations=results)

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary:
        post_ids = [int(value) for value in payload.get("representative_post_ids", [])]
        median = payload.get("median_caption_words", "unknown")
        return StyleSummary(
            summary=(
                "This fixture channel favors compact, playful reaction captions "
                f"near {median} words, "
                "with questions used selectively and images framed around one clear emotional beat."
            ),
            cited_post_ids=post_ids[:5],
            rotation_observations=[
                "Alternate centered reactions with comparison compositions.",
                "Avoid repeating the same fixture topic on consecutive days.",
            ],
        )

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan:
        topic = str(
            payload.get("primary_topic") or payload.get("primary_franchise") or "channel subject"
        )
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
            desired_entities=[],
            desired_topics=[topic],
            desired_actions=["clear visible action", "expressive reaction"],
            desired_scenes=[],
            desired_compositions=["close-up", "group scene", "wide composition"],
            source_policy=str(payload.get("source_policy", "preserve_and_review")),
            rights_policy=str(payload.get("rights_policy", "unknown_requires_review")),
        )

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        candidate_id = int(payload.get("candidate_id", 0))
        subject = f"Fixture subject {(candidate_id % 7) + 1}"
        concepts = [
            ("reaction", "centered close up", "surprise", "living room", "reacting"),
            ("group decision", "balanced two-subject", "determination", "office", "choosing"),
            ("shared meal", "wide group shot", "happiness", "kitchen", "eating"),
            ("outdoor activity", "dynamic full shot", "excitement", "park", "running"),
            ("performance", "stage medium shot", "confidence", "theatre", "performing"),
            ("workshop repair", "over-shoulder view", "curiosity", "workshop", "repairing"),
            ("public celebration", "crowd wide shot", "joy", "plaza", "celebrating"),
            ("quiet study", "desk close up", "concentration", "library", "writing"),
            ("travel departure", "doorway medium shot", "anticipation", "station", "boarding"),
            ("sideline strategy", "layered group shot", "focus", "sports field", "coaching"),
            ("dark search", "flashlight point of view", "concern", "basement", "searching"),
            ("music rehearsal", "ensemble medium shot", "delight", "studio", "playing music"),
        ]
        scene, composition, emotion, setting, action = concepts[candidate_id % len(concepts)]
        franchise_index = (candidate_id + candidate_id // len(concepts)) % 3
        return CandidateAnalysis(
            franchise=["Synthetic Ensemble", "Synthetic Adventure", "Synthetic Comedy"][
                franchise_index
            ],
            characters=[subject],
            scene_archetype=scene,
            composition=composition,
            emotion=emotion,
            text_overlay=False,
            watermark_probability=0.02,
            unsafe_probability=0.0,
            personal_artwork_probability=0.0,
            fan_art_probability=0.0,
            caption_potential=0.78 + (candidate_id % 5) * 0.03,
            confidence=0.9,
            entities=[
                {
                    "name": subject,
                    "entity_type": "fictional_character",
                    "confidence": 0.9,
                    "canonical_name": subject,
                }
            ],
            objects=["fixture prop"],
            actions=[action],
            relationships=["subjects share the visible scene"] if candidate_id % 2 == 0 else [],
            setting=setting,
            ocr_text=[],
            field_confidence={
                "entities": 0.9,
                "emotion": 0.88,
                "actions": 0.84,
                "scene": 0.9,
                "ocr": 0.95,
            },
        )

    async def audit_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        return await self.analyze_candidate_image(payload)

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionCandidateSet:
        candidate_id = int(payload.get("candidate_id", 0))
        references = [int(value) for value in payload.get("historical_post_ids", [])][:4]
        analysis = payload.get("candidate_analysis", {})
        analysis_row = analysis if isinstance(analysis, Mapping) else {}
        characters = analysis_row.get("characters", [])
        raw_subject = (
            str(characters[0])
            if isinstance(characters, list) and characters
            else f"this scene {candidate_id}"
        )
        subject = _mock_subject_label(raw_subject)
        emotion = _mock_emotion_label(str(analysis_row.get("emotion") or "curious").strip())
        action_values = analysis_row.get("actions", [])
        action = (
            str(action_values[0])
            if isinstance(action_values, list) and action_values
            else "reacting"
        )
        subject_start = subject[:1].upper() + subject[1:]
        possessive = f"{subject}'" if subject.endswith("s") else f"{subject}'s"
        possessive_start = possessive[:1].upper() + possessive[1:]
        brief = payload.get("editorial_brief", {})
        brief_row = brief if isinstance(brief, Mapping) else {}
        raw_lanes = payload.get("angle_lanes", [])
        angle_lanes = [
            str(value.get("lane") or "").strip()
            for value in raw_lanes
            if isinstance(value, Mapping) and str(value.get("lane") or "").strip()
        ]
        structures = [
            str(value)
            for value in brief_row.get(
                "target_structures",
                ["open_question", "observation", "reaction"],
            )
        ]
        templates: dict[str, list[str]] = {
            "open_question": [
                f"Why is {subject} so {emotion}?",
                f"What could explain {possessive} {emotion} reaction?",
                f"How would you caption {possessive} {emotion} reaction?",
            ],
            "yes_no_question": [f"Is {subject} ready for this?"],
            "observation": [
                f"{possessive_start} {emotion} reaction says plenty.",
                f"Every detail points back to {subject}.",
            ],
            "reaction": [
                f"That {emotion} look needs no explanation.",
                f"{subject_start} has entered the chat.",
            ],
            "comparison": [f"{subject_start} versus the plan: place your bets."],
            "prediction": [f"{subject_start} is about to make this interesting."],
            "fill_in_blank": [f"{subject_start} is reacting to ____."],
            "poll": [f"Pick {possessive} next move: stay or go?"],
            "quiz": [f"Can you name what {subject} noticed?"],
            "call_to_action": [f"Choose the best explanation for {subject}'s reaction."],
            "explanation": [f"This scene centers on {subject} and the visible action of {action}."],
            "promotional_statement": [f"Introducing {possessive} most {emotion} moment."],
            "quote_or_reference": [f"“{subject_start} looks {emotion}.”"],
        }
        candidates: list[CaptionCandidate] = []
        lane_structures = {
            "audience_inquiry": "open_question",
            "visible_reaction": "reaction",
            "visual_observation": "observation",
            "visible_action": "prediction",
            "visible_relationship": "comparison",
            "object_focus": "observation",
        }
        for lane in angle_lanes:
            structure = lane_structures.get(lane, "observation")
            text = templates.get(structure, templates["observation"])[0]
            if text in {candidate.text for candidate in candidates}:
                text = templates.get(structure, templates["observation"])[-1]
            candidates.append(
                CaptionCandidate(
                    text=text,
                    structure=structure,
                    language=str(brief_row.get("target_language", "en")),
                    editorial_angle=lane,
                    visible_evidence=[subject, emotion, action],
                    uncertainty=[],
                    historical_evidence=references,
                    feedback_evidence=[],
                    confidence=0.84,
                )
            )
        for structure in structures:
            for text in templates.get(structure, templates["observation"]):
                candidates.append(
                    CaptionCandidate(
                        text=text,
                        structure=structure,
                        language=str(brief_row.get("target_language", "en")),
                        editorial_angle=(
                            "audience_inquiry" if structure.endswith("question") else structure
                        ),
                        visible_evidence=[subject, emotion, action],
                        uncertainty=[],
                        historical_evidence=references,
                        feedback_evidence=[],
                        confidence=0.84,
                    )
                )
                if len(candidates) >= 9:
                    break
            if len(candidates) >= 9:
                break
        for fallback_structure in ("open_question", "observation", "reaction"):
            if len(candidates) >= 6:
                break
            for text in templates[fallback_structure]:
                if text not in {candidate.text for candidate in candidates}:
                    candidates.append(
                        CaptionCandidate(
                            text=text,
                            structure=fallback_structure,
                            language=str(brief_row.get("target_language", "en")),
                            editorial_angle=fallback_structure,
                            visible_evidence=[subject, emotion, action],
                            uncertainty=[],
                            historical_evidence=references,
                            feedback_evidence=[],
                            confidence=0.82,
                        )
                    )
                if len(candidates) >= 6:
                    break
        return CaptionCandidateSet(
            candidates=candidates,
            rationale="Uses the channel brief and only deterministic visible fixture facts.",
            confidence=0.86,
            referenced_historical_post_ids=references,
            factual_uncertainty_warning=None,
        )

    async def verify_caption_grounding(self, payload: Mapping[str, Any]) -> CaptionGroundingAudit:
        source_text = json.dumps(payload.get("trusted_source_evidence", {})).casefold()
        assessments: list[CaptionGroundingAssessment] = []
        for raw in payload.get("candidates", []):
            if not isinstance(raw, Mapping):
                continue
            candidate_id = int(raw.get("candidate_id", 0))
            text = str(raw.get("text") or "").strip()
            lowered = text.casefold()
            contradicted = "cracker" in source_text and any(
                marker in lowered for marker in ("newspaper", "reading", "read ")
            )
            assessments.append(
                CaptionGroundingAssessment(
                    candidate_id=candidate_id,
                    verdict="unsupported" if contradicted else "supported",
                    grounding_score=0.0 if contradicted else 0.95,
                    factual_claims=[text] if text else [],
                    supported_claims=[] if contradicted else ([text] if text else []),
                    uncertain_claims=[],
                    unsupported_claims=[text] if contradicted else [],
                    visual_evidence=[] if contradicted else ["deterministic fixture evidence"],
                    source_evidence=["cracker source context"] if contradicted else [],
                    contradictions=["caption conflicts with trusted source context"]
                    if contradicted
                    else [],
                    corrected_caption=None,
                    confidence=0.95,
                )
            )
        return CaptionGroundingAudit(
            assessments=assessments,
            image_summary="Deterministic fixture image audit.",
            source_context_used=["trusted_source_evidence"] if source_text != "{}" else [],
            audit_confidence=0.95,
        )

    async def simulate_editorial_decision(
        self, payload: Mapping[str, Any]
    ) -> ShadowEditorialRecommendation:
        caption = str(payload.get("final_caption") or "").strip()
        candidate = payload.get("candidate", {})
        candidate_row = candidate if isinstance(candidate, Mapping) else {}
        analysis = candidate_row.get("analysis", {})
        analysis_row = analysis if isinstance(analysis, Mapping) else {}
        image_score = float(candidate_row.get("final_rank_score") or 0.5)
        question_first = caption.endswith("?") and caption.casefold().startswith(
            ("how ", "what ", "why ", "which ", "who ")
        )
        caption_score = 0.82 if question_first else 0.56
        pairing_score = min(1.0, 0.55 * image_score + 0.45 * caption_score)
        unsafe = float(analysis_row.get("unsafe_probability") or 0.0)
        characters = analysis_row.get("characters", [])
        subject = (
            _mock_subject_label(str(characters[0]))
            if isinstance(characters, list) and characters
            else "the subject"
        )
        if unsafe >= 0.5:
            decision = "reject"
            edited_caption = None
            reasons = ["nsfw"]
            rationale = "The image is not eligible under the NSFW safeguard."
        elif image_score < 0.5:
            decision = "reject"
            edited_caption = None
            reasons = ["weak_image"]
            rationale = "The image is below the learned editorial quality threshold."
        elif not question_first:
            decision = "edit"
            edited_caption = f"What has {subject} reacting like this?"
            reasons = ["prefer_open_question"]
            rationale = "The image is usable, but an open-ended question better matches feedback."
        else:
            decision = "accept"
            edited_caption = None
            reasons = ["open_question", "strong_pairing"]
            rationale = "The image and grounded open-ended question form a strong pairing."
        return ShadowEditorialRecommendation(
            decision=decision,
            edited_caption=edited_caption,
            image_score=max(0.0, min(1.0, image_score)),
            caption_score=caption_score,
            pairing_score=pairing_score,
            confidence=0.72,
            reason_codes=reasons,
            rationale=rationale,
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
        public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        image_paths = _validated_image_paths(payload)

        def request() -> SchemaT:
            content: list[dict[str, str]] = [
                {
                    "type": "input_text",
                    "text": json.dumps(public_payload, default=str),
                }
            ]
            for image_path in image_paths:
                encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
                mime_type = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                }.get(image_path.suffix.lower())
                if mime_type is None:
                    raise ValueError(f"unsupported model image type: {image_path.suffix}")
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime_type};base64,{encoded}",
                    }
                )
            response = self.client.responses.parse(
                model=self.model_name,
                input=cast(
                    Any,
                    [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": content},
                    ],
                ),
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

    async def annotate_historical_posts(
        self, payload: Mapping[str, Any]
    ) -> HistoricalAnnotationBatch:
        return await self._parse(
            HistoricalAnnotationBatch,
            "annotate-history-batch-v3.txt",
            payload,
        )

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary:
        return await self._parse(StyleSummary, "style-summary-v1.txt", payload)

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan:
        return await self._parse(SearchPlan, "search-plan-v2.txt", payload)

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        return await self._parse(CandidateAnalysis, "candidate-analysis-v3.txt", payload)

    async def audit_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        return await self._parse(CandidateAnalysis, "candidate-analysis-blind-v1.txt", payload)

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionCandidateSet:
        return await self._parse(CaptionCandidateSet, "captions-v6.txt", payload)

    async def verify_caption_grounding(self, payload: Mapping[str, Any]) -> CaptionGroundingAudit:
        return await self._parse(CaptionGroundingAudit, "caption-grounding-v1.txt", payload)

    async def simulate_editorial_decision(
        self, payload: Mapping[str, Any]
    ) -> ShadowEditorialRecommendation:
        return await self._parse(
            ShadowEditorialRecommendation,
            "shadow-editorial-v1.txt",
            payload,
        )


class CodexAgentRuntime:
    """ChatGPT-authenticated Codex CLI runtime with no paid API fallback."""

    provider = "codex-chatgpt"
    _execution_lock = threading.Lock()
    _usage_limit_markers = (
        "usage limit",
        "rate limit",
        "limit reached",
        "you've hit your limit",
        "you have hit your limit",
        "insufficient quota",
        "out of credits",
    )
    _authentication_markers = (
        "not logged in",
        "login required",
        "authentication required",
        "unauthorized",
        "refresh token",
        "please run codex login",
    )
    _capacity_markers = (
        "model is at capacity",
        "selected model is at capacity",
        "service is overloaded",
        "temporarily overloaded",
    )
    _capacity_retry_delays = (5.0, 15.0, 30.0)

    def __init__(
        self,
        settings: Settings,
        *,
        command_prefix: list[str] | None = None,
    ):
        self.settings = settings
        self.model_name = settings.codex_model
        self.reasoning_effort = settings.codex_reasoning_effort
        self.timeout_seconds = settings.codex_timeout_seconds
        self.prompts = Path(__file__).resolve().parent / "prompts"
        self.last_token_usage: dict[str, object] = {}
        self.command_prefix = command_prefix or self.resolve_command(settings)

    @staticmethod
    def resolve_command(settings: Settings) -> list[str]:
        configured = settings.codex_cli_path
        if configured is not None:
            path = configured.expanduser().resolve()
            if not path.is_file():
                raise AgentAuthenticationRequired(f"configured Codex CLI was not found: {path}")
            if path.suffix.lower() == ".js":
                node = shutil.which("node")
                if node is None:
                    raise AgentAuthenticationRequired("Node.js is required for the Codex CLI")
                return [node, str(path)]
            return [str(path)]

        local_script = (
            settings.project_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        )
        node = shutil.which("node")
        if local_script.is_file() and node is not None:
            return [node, str(local_script)]

        executable = shutil.which("codex")
        if executable is not None:
            return [executable]
        raise AgentAuthenticationRequired(
            "Codex CLI is missing; run `npm install`, then `runway agent login`"
        )

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        environment = os.environ.copy()
        for secret_name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            environment.pop(secret_name, None)
        return environment

    def login_status(self) -> str:
        try:
            result = subprocess.run(
                [*self.command_prefix, "login", "status"],
                cwd=self.settings.project_root,
                env=self._safe_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentAuthenticationRequired(f"could not check Codex login: {exc}") from exc
        detail = "\n".join(value for value in (result.stdout, result.stderr) if value).strip()
        lowered = detail.lower()
        if result.returncode != 0:
            raise AgentAuthenticationRequired(detail or "Codex login status failed")
        if "api key" in lowered:
            raise PaidApiAuthenticationBlocked(
                "Codex is authenticated with an API key; run `runway agent login` "
                "and choose Sign in with ChatGPT"
            )
        if "chatgpt" not in lowered:
            raise AgentAuthenticationRequired(
                detail or "Codex must be authenticated with the paid ChatGPT account"
            )
        return detail

    def login_interactive(self) -> int:
        result = subprocess.run(
            [*self.command_prefix, "login"],
            cwd=self.settings.project_root,
            env=self._safe_environment(),
            check=False,
        )
        return result.returncode

    def version(self) -> str:
        try:
            result = subprocess.run(
                [*self.command_prefix, "--version"],
                cwd=self.settings.project_root,
                env=self._safe_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentRuntimeError(f"could not run Codex CLI: {exc}") from exc
        detail = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            raise AgentRuntimeError(detail or "Codex version check failed")
        return detail

    async def _parse(
        self,
        schema: type[SchemaT],
        prompt_name: str,
        payload: Mapping[str, Any],
        *,
        require_image: bool = False,
    ) -> SchemaT:
        return await asyncio.to_thread(
            self._parse_sync,
            schema,
            prompt_name,
            payload,
            require_image,
        )

    def _parse_sync(
        self,
        schema: type[SchemaT],
        prompt_name: str,
        payload: Mapping[str, Any],
        require_image: bool,
    ) -> SchemaT:
        with self._execution_lock:
            self.login_status()
            image_paths = _validated_image_paths(payload)
            if require_image and not image_paths:
                raise AgentRuntimeError("this Codex task requires a local image file")
            public_payload = {
                key: value for key, value in payload.items() if not key.startswith("_")
            }
            instructions = (self.prompts / prompt_name).read_text(encoding="utf-8")
            prompt = (
                "Complete only the requested analysis task. Do not run commands, browse, "
                "inspect repository files, edit files, or call external tools. Return only the "
                "JSON object required by the supplied schema.\n\n"
                f"Task instructions:\n{instructions.strip()}\n\n"
                f"Bounded Runway input:\n{json.dumps(public_payload, default=str, sort_keys=True)}"
            )
            work_dir = self.settings.resolved_data_dir / "raw" / "codex-runtime"
            work_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="request-", dir=work_dir) as temporary:
                temporary_path = Path(temporary)
                schema_path = temporary_path / "schema.json"
                output_path = temporary_path / "output.json"
                schema_path.write_text(
                    json.dumps(schema.model_json_schema(), sort_keys=True),
                    encoding="utf-8",
                )
                command = [
                    *self.command_prefix,
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--model",
                    self.model_name,
                    "--config",
                    f'model_reasoning_effort="{self.reasoning_effort}"',
                    "--config",
                    'web_search="disabled"',
                    "--json",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                ]
                for image_path in image_paths:
                    command.extend(["--image", str(image_path)])
                command.append("-")
                result: subprocess.CompletedProcess[str] | None = None
                combined = ""
                for attempt in range(len(self._capacity_retry_delays) + 1):
                    output_path.unlink(missing_ok=True)
                    try:
                        result = subprocess.run(
                            command,
                            cwd=temporary_path,
                            env=self._safe_environment(),
                            input=prompt,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            check=False,
                            timeout=self.timeout_seconds,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise AgentTerminalError(
                            f"Codex exceeded the {self.timeout_seconds}-second timeout; "
                            "batch stopped"
                        ) from exc
                    self.last_token_usage = self._extract_usage(result.stdout)
                    combined = "\n".join(
                        value for value in (result.stdout, result.stderr) if value
                    ).strip()
                    if result.returncode == 0:
                        break
                    if self._is_capacity_error(combined) and attempt < len(
                        self._capacity_retry_delays
                    ):
                        time.sleep(self._capacity_retry_delays[attempt])
                        continue
                    self._raise_classified_error(combined)
                if result is None:
                    raise AgentTerminalError("Codex did not start; batch stopped")
                if not output_path.is_file():
                    raise AgentTerminalError("Codex returned no structured output; batch stopped")
                try:
                    return schema.model_validate_json(output_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise AgentTerminalError(
                        f"Codex returned invalid structured output: {exc}"
                    ) from exc

    @staticmethod
    def _extract_usage(stdout: str) -> dict[str, object]:
        usage: dict[str, object] = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = dict(event["usage"])
        return usage

    def _raise_classified_error(self, detail: str) -> None:
        lowered = detail.lower()
        if any(marker in lowered for marker in self._usage_limit_markers):
            raise AgentUsageLimitReached(
                "ChatGPT/Codex included usage limit reached; Runway stopped without API fallback"
            )
        if any(marker in lowered for marker in self._authentication_markers):
            raise AgentAuthenticationRequired(
                "Codex authentication expired; run `runway agent login` and retry"
            )
        if "api key" in lowered or "billing" in lowered:
            raise PaidApiAuthenticationBlocked(
                "Codex requested paid API authentication; Runway stopped"
            )
        if self._is_capacity_error(detail):
            raise AgentTerminalError(
                "Codex model capacity remained unavailable after bounded retries; "
                "batch stopped without API fallback"
            )
        summary = detail[-2000:] if detail else "unknown Codex CLI failure"
        raise AgentTerminalError(f"Codex failed and the batch was stopped: {summary}")

    @classmethod
    def _is_capacity_error(cls, detail: str) -> bool:
        lowered = detail.casefold()
        return any(marker in lowered for marker in cls._capacity_markers)

    async def annotate_historical_post(self, payload: Mapping[str, Any]) -> HistoricalAnnotation:
        return await self._parse(
            HistoricalAnnotation,
            "annotate-history-v1.txt",
            payload,
            require_image=True,
        )

    async def annotate_historical_posts(
        self, payload: Mapping[str, Any]
    ) -> HistoricalAnnotationBatch:
        return await self._parse(
            HistoricalAnnotationBatch,
            "annotate-history-batch-v3.txt",
            payload,
            require_image=True,
        )

    async def build_style_summary(self, payload: Mapping[str, Any]) -> StyleSummary:
        return await self._parse(StyleSummary, "style-summary-v1.txt", payload)

    async def create_search_plan(self, payload: Mapping[str, Any]) -> SearchPlan:
        return await self._parse(SearchPlan, "search-plan-v2.txt", payload)

    async def analyze_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        return await self._parse(
            CandidateAnalysis,
            "candidate-analysis-v3.txt",
            payload,
            require_image=True,
        )

    async def audit_candidate_image(self, payload: Mapping[str, Any]) -> CandidateAnalysis:
        return await self._parse(
            CandidateAnalysis,
            "candidate-analysis-blind-v1.txt",
            payload,
            require_image=True,
        )

    async def generate_caption_options(self, payload: Mapping[str, Any]) -> CaptionCandidateSet:
        return await self._parse(
            CaptionCandidateSet,
            "captions-v6.txt",
            payload,
            require_image=True,
        )

    async def verify_caption_grounding(self, payload: Mapping[str, Any]) -> CaptionGroundingAudit:
        return await self._parse(
            CaptionGroundingAudit,
            "caption-grounding-v1.txt",
            payload,
            require_image=True,
        )

    async def simulate_editorial_decision(
        self, payload: Mapping[str, Any]
    ) -> ShadowEditorialRecommendation:
        return await self._parse(
            ShadowEditorialRecommendation,
            "shadow-editorial-v1.txt",
            payload,
            require_image=True,
        )


def _validated_image_path(value: object) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        raise AgentRuntimeError(f"model image file does not exist: {path}")
    return path


def _validated_image_paths(payload: Mapping[str, Any]) -> list[Path]:
    paths: list[Path] = []
    single = _validated_image_path(payload.get("_image_path"))
    if single is not None:
        paths.append(single)
    multiple = payload.get("_image_paths", [])
    if multiple in (None, ""):
        return paths
    if not isinstance(multiple, (list, tuple)):
        raise AgentRuntimeError("_image_paths must be a list of local image files")
    for value in multiple:
        path = _validated_image_path(value)
        if path is not None:
            paths.append(path)
    return paths


def runtime_for(settings: Settings) -> AgentRuntime:
    if settings.agent_runtime == "codex":
        return CodexAgentRuntime(settings)
    if settings.agent_runtime == "openai":
        return OpenAIAgentRuntime(settings)
    return MockAgentRuntime()
