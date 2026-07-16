from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoricalAnnotation(StrictModel):
    franchise: str | None = None
    show_name: str | None = None
    visible_characters: list[str] = Field(default_factory=list)
    visible_character_count: int = Field(ge=0)
    scene_description: str
    visual_medium: str
    composition: str
    facial_emotional_cues: str
    text_overlay: bool
    reaction_potential: str
    caption_intent: str
    caption_structure: str
    humor_style: str
    tone: str
    confidence: dict[str, float]


class StyleSummary(StrictModel):
    summary: str
    cited_post_ids: list[int]
    rotation_observations: list[str]


class SearchQueryFamily(StrictModel):
    purpose: str
    queries: list[str]


class SearchPlan(StrictModel):
    query_families: list[SearchQueryFamily]
    desired_visual_traits: list[str]
    excluded_concepts: list[str]


class CandidateAnalysis(StrictModel):
    franchise: str | None = None
    characters: list[str] = Field(default_factory=list)
    scene_archetype: str
    composition: str
    emotion: str
    text_overlay: bool
    watermark_probability: float = Field(ge=0, le=1)
    unsafe_probability: float = Field(ge=0, le=1)
    caption_potential: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


class CaptionOptions(StrictModel):
    recommended: str
    alternatives: list[str] = Field(min_length=2, max_length=2)
    rationale: str
    confidence: float = Field(ge=0, le=1)
    referenced_historical_post_ids: list[int] = Field(default_factory=list)
    factual_uncertainty_warning: str | None = None
