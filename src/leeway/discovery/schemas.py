from __future__ import annotations

from pydantic import BaseModel, Field


class ImageSearchResult(BaseModel):
    search_query: str
    result_rank: int = Field(ge=1)
    source_page_url: str
    direct_image_url: str
    source_domain: str
    original_width: int | None = None
    original_height: int | None = None
    rights_status: str = "unknown"
    provider_metadata: dict[str, object] = Field(default_factory=dict)


class SearchPage(BaseModel):
    results: list[ImageSearchResult]
    next_cursor: str | None = None
