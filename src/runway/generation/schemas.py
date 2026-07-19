from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GenerationCapability = Literal[
    "text_to_image",
    "reference_edit",
    "variation",
]


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: set[GenerationCapability]
    maximum_references: int = Field(ge=0, le=16)
    supports_seed: bool
    local_only: bool
    paid_usage: bool


class CreativeBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: GenerationCapability
    instruction: str = Field(min_length=3, max_length=2000)
    negative_instruction: str = Field(default="", max_length=1000)
    reference_media_ids: list[int] = Field(default_factory=list, max_length=8)
    explicitly_approved_reference_ids: list[int] = Field(
        default_factory=list,
        max_length=8,
    )
    consent_state: Literal[
        "not_required",
        "creator_confirmed",
        "missing",
    ] = "not_required"
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    width: int = Field(default=1024, ge=480, le=2048)
    height: int = Field(default=1024, ge=480, le=2048)
    parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> CreativeBrief:
        reference_ids = set(self.reference_media_ids)
        if not set(self.explicitly_approved_reference_ids).issubset(reference_ids):
            raise ValueError("approved references must also appear in reference_media_ids")
        if self.capability in {"reference_edit", "variation"} and not reference_ids:
            raise ValueError(f"{self.capability} requires at least one reference")
        if self.capability == "text_to_image" and reference_ids:
            raise ValueError("text_to_image cannot silently accept image references")
        return self


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_identity: str
    brief: CreativeBrief
    reference_paths: list[Path]
    output_directory: Path
    output_count: int = Field(default=1, ge=1, le=4)


class ProviderImage(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    path: Path
    seed: int
    provider_metadata: dict[str, object] = Field(default_factory=dict)
    safety_result: dict[str, object] = Field(default_factory=dict)
