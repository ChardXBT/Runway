"""Request and response shapes of the posts API (spec 03 section 8).

Shared by the `/api/v1/posts` routes and by any other caller of
`runway.posts.service.PostService` (the agent interface), so both speak the
same contract. Times are RFC 3339 UTC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from runway.platforms.capabilities import Platform


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetInput(_Input):
    platform: Platform
    account_id: int | None = Field(default=None, ge=1)
    post_type: str | None = Field(default=None, max_length=40)
    caption_override: str | None = Field(default=None, max_length=20_000)
    settings: dict[str, Any] = Field(default_factory=dict)
    planned_at: datetime | None = None


class PostCreate(_Input):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=20_000)
    media_ids: list[int] = Field(default_factory=list, max_length=20)
    targets: list[TargetInput] = Field(default_factory=list, max_length=20)
    scheduled_for: datetime | None = None
    notes: str | None = Field(default=None, max_length=5_000)
    # Posts created by an agent key default to true (spec 06 section 4).
    ai_generated: bool | None = None


class PostPatch(_Input):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, max_length=20_000)
    media_ids: list[int] | None = Field(default=None, max_length=20)
    scheduled_for: datetime | None = None
    clear_schedule: bool = False
    notes: str | None = Field(default=None, max_length=5_000)
    ai_generated: bool | None = None


class TargetUpdate(_Input):
    """`PUT /posts/{id}/targets/{tid}`: the target's full editable state."""

    post_type: str | None = Field(default=None, max_length=40)
    account_id: int | None = Field(default=None, ge=1)
    caption_override: str | None = Field(default=None, max_length=20_000)
    settings: dict[str, Any] = Field(default_factory=dict)


class ScheduleRequest(_Input):
    """Either a time (`planned_at`) or `now: true`."""

    planned_at: datetime | None = None
    now: bool = False


class ReleaseRequest(_Input):
    # The hash the person saw in the release dialog; must match the current one.
    payload_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    confirmed: bool
    # TikTok's mandatory screen ends in an express consent: the request echoes
    # the exact line the dialog showed (`release_notice` on the target).
    consent: str | None = Field(default=None, max_length=2_000)


class MarkPublishedRequest(_Input):
    published_url: str = Field(min_length=8, max_length=2_000)
    external_id: str | None = Field(default=None, max_length=200)


class ReconcileRequest(_Input):
    """`check` asks the platform again; `not_published` is a person's own finding."""

    resolution: Literal["check", "not_published"] = "check"
    confirmed: bool = False


class CrossPostRequest(_Input):
    targets: list[TargetInput] = Field(min_length=1, max_length=20)
    title: str | None = Field(default=None, min_length=1, max_length=500)


# ---------------------------------------------------------------- responses


class IssueOut(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    fix: str | None = None
    source: str | None = None
    item: int | None = None
    field: str | None = None


class CapabilityOut(BaseModel):
    post_type: str
    state: str
    detail: str
    direct_adapter: str
    default_publisher: str
    missing_scopes: list[str]
    available_publishers: list[str]


class ReleaseOut(BaseModel):
    state: Literal["none", "released"]
    released_by: str | None
    released_at: datetime | None
    payload_hash: str | None


class AttemptOut(BaseModel):
    id: int
    target_id: int
    job_id: int | None
    adapter: str
    kind: str
    payload_hash: str | None
    started_at: datetime
    request_sent_at: datetime | None
    finished_at: datetime | None
    outcome: str
    external_id: str | None
    error_code: str | None
    error_detail: str | None
    response: dict[str, Any]


class TargetOut(BaseModel):
    id: int
    post_id: int
    platform: str
    account_id: int | None
    post_type: str | None
    caption_override: str | None
    caption: str
    settings: dict[str, Any]
    effective_settings: dict[str, Any]
    planned_at: datetime | None
    state: str
    state_reason: str | None
    version: int
    release: ReleaseOut
    # What a release of the target right now approves, and its hash.
    payload: dict[str, Any]
    payload_hash: str
    publisher: str | None
    capability: CapabilityOut | None
    issues: list[IssueOut]
    releasable: bool
    privacy_in_words: str | None
    release_notice: str | None
    external_id: str | None
    published_url: str | None
    published_at: datetime | None
    last_attempt: AttemptOut | None


class MediaRefOut(BaseModel):
    asset_id: int
    position: int
    kind: str | None
    status: str
    sha256: str
    size_bytes: int


class PostOut(BaseModel):
    id: int
    title: str
    body: str
    notes: str | None
    source: str
    source_ref: str | None
    ai_generated: bool
    created_by: str | None
    version: int
    # Derived from the targets by one function; never stored (spec 03 section 2).
    status: str
    scheduled_for: datetime | None
    media: list[MediaRefOut]
    targets: list[TargetOut]
    created_at: datetime
    updated_at: datetime
