from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoricalConfidence(StrictModel):
    franchise: float = Field(ge=0, le=1)
    characters: float = Field(ge=0, le=1)
    scene: float = Field(ge=0, le=1)
    caption: float = Field(ge=0, le=1)
    entities: float = Field(ge=0, le=1)
    actions: float = Field(ge=0, le=1)
    relationships: float = Field(ge=0, le=1)
    ocr: float = Field(ge=0, le=1)


class VisualEntity(StrictModel):
    name: str
    entity_type: str
    confidence: float = Field(ge=0, le=1)
    canonical_name: str | None


class HistoricalAnnotation(StrictModel):
    franchise: str | None
    show_name: str | None
    visible_characters: list[str]
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
    confidence: HistoricalConfidence
    entities: list[VisualEntity]
    people: list[str]
    organizations: list[str]
    products: list[str]
    teams: list[str]
    locations: list[str]
    animals: list[str]
    objects: list[str]
    actions: list[str]
    relationships: list[str]
    setting: str
    ocr_text: list[str]
    editorial_angle: str
    audience_invitation_type: str
    image_caption_relationship: str


class HistoricalAnnotationResult(StrictModel):
    post_id: int = Field(ge=1)
    annotation: HistoricalAnnotation


class HistoricalAnnotationBatch(StrictModel):
    annotations: list[HistoricalAnnotationResult] = Field(min_length=1)


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
    desired_entities: list[str]
    desired_topics: list[str]
    desired_actions: list[str]
    desired_scenes: list[str]
    desired_compositions: list[str]
    source_policy: str
    rights_policy: str


class CandidateFieldConfidence(StrictModel):
    entities: float = Field(ge=0, le=1)
    emotion: float = Field(ge=0, le=1)
    actions: float = Field(ge=0, le=1)
    scene: float = Field(ge=0, le=1)
    ocr: float = Field(ge=0, le=1)


class CandidateAnalysis(StrictModel):
    franchise: str | None
    characters: list[str]
    scene_archetype: str
    composition: str
    emotion: str
    text_overlay: bool
    watermark_probability: float = Field(ge=0, le=1)
    unsafe_probability: float = Field(ge=0, le=1)
    personal_artwork_probability: float = Field(ge=0, le=1)
    fan_art_probability: float = Field(ge=0, le=1)
    caption_potential: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    entities: list[VisualEntity]
    objects: list[str]
    actions: list[str]
    relationships: list[str]
    setting: str
    ocr_text: list[str]
    field_confidence: CandidateFieldConfidence


class CaptionGroundingAssessment(StrictModel):
    candidate_id: int = Field(ge=1)
    verdict: Literal["supported", "uncertain", "unsupported"]
    grounding_score: float = Field(ge=0, le=1)
    factual_claims: list[str] = Field(min_length=1)
    supported_claims: list[str]
    uncertain_claims: list[str]
    unsupported_claims: list[str]
    visual_evidence: list[str]
    source_evidence: list[str]
    contradictions: list[str]
    corrected_caption: str | None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_verdict(self) -> CaptionGroundingAssessment:
        if any(not value.strip() for value in self.factual_claims):
            raise ValueError("caption grounding factual claims cannot be blank")
        if self.unsupported_claims or self.contradictions:
            self.verdict = "unsupported"
            self.grounding_score = min(self.grounding_score, 0.2)
        elif self.uncertain_claims:
            self.verdict = "uncertain"
            self.grounding_score = min(self.grounding_score, 0.6)
        if self.verdict == "uncertain" and not self.uncertain_claims:
            raise ValueError("an uncertain caption must identify the uncertain claim")
        if self.verdict == "unsupported" and not (self.unsupported_claims or self.contradictions):
            raise ValueError("an unsupported caption must identify the failed claim")
        if self.verdict == "supported" and not self.supported_claims:
            raise ValueError("a supported caption must identify at least one supported claim")
        return self


class CaptionGroundingAudit(StrictModel):
    assessments: list[CaptionGroundingAssessment] = Field(min_length=1, max_length=12)
    image_summary: str
    source_context_used: list[str]
    audit_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_candidate_ids(self) -> CaptionGroundingAudit:
        candidate_ids = [assessment.candidate_id for assessment in self.assessments]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("caption grounding candidate IDs must be unique")
        return self


class ShadowEditorialRecommendation(StrictModel):
    decision: Literal["accept", "edit", "reject", "abstain"]
    edited_caption: str | None
    image_score: float = Field(ge=0, le=1)
    caption_score: float = Field(ge=0, le=1)
    pairing_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str]
    rationale: str


class CaptionOptions(StrictModel):
    recommended: str
    alternatives: list[str] = Field(max_length=2)
    rationale: str
    confidence: float = Field(ge=0, le=1)
    referenced_historical_post_ids: list[int]
    factual_uncertainty_warning: str | None
    slate_id: int | None = None
    retrieval_run_id: int | None = None
    abstained: bool = False
    abstention_reason: str | None = None

    @model_validator(mode="after")
    def validate_slate_shape(self) -> CaptionOptions:
        if self.abstained:
            if self.recommended or self.alternatives:
                raise ValueError("an abstained caption slate cannot expose captions")
            return self
        if len(self.alternatives) < 1:
            raise ValueError("a completed caption slate requires at least one alternative")
        return self


class CaptionCandidate(StrictModel):
    text: str = Field(min_length=1, max_length=280)
    structure: Literal[
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
    ]
    language: str
    editorial_angle: str
    visible_evidence: list[str]
    uncertainty: list[str]
    historical_evidence: list[int]
    feedback_evidence: list[int]
    confidence: float = Field(ge=0, le=1)


class CaptionCandidateSet(StrictModel):
    candidates: list[CaptionCandidate] = Field(min_length=3, max_length=12)
    rationale: str
    confidence: float = Field(ge=0, le=1)
    referenced_historical_post_ids: list[int]
    factual_uncertainty_warning: str | None
