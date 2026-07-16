from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from leeway.domain.enums import DatePrecision, PostType


class ImageReference(BaseModel):
    url: str
    alt_text: str | None = None
    position: int = 0


class ExtractedPost(BaseModel):
    external_post_id: str | None = None
    permalink: str | None = None
    post_type: PostType = PostType.UNKNOWN
    caption: str | None = None
    displayed_date_text: str | None = None
    published_at: datetime | None = None
    date_precision: DatePrecision = DatePrecision.UNKNOWN
    like_count: int | None = None
    comment_count: int | None = None
    raw_like_text: str | None = None
    raw_comment_text: str | None = None
    images: list[ImageReference] = Field(default_factory=list)
    raw: dict[str, object] = Field(default_factory=dict)

    def stable_key(self) -> str:
        return self.external_post_id or self.permalink or ""


class ExtractionDiagnostic(BaseModel):
    code: str
    message: str
    snippet: str | None = None


class ExtractionBatch(BaseModel):
    posts: list[ExtractedPost]
    diagnostics: list[ExtractionDiagnostic] = Field(default_factory=list)
