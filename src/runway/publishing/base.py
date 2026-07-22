from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class PublisherSessionStatus(BaseModel):
    valid: bool
    publisher: str
    detail: str
    checks: dict[str, bool] = Field(default_factory=dict)


class PreparedPost(BaseModel):
    proposal_id: int
    planned_publish_at: str
    caption: str
    local_image_path: str


class PublishResult(BaseModel):
    proposal_id: int
    status: str
    external_id: str | None = None
    detail: str


class PublishPreparation(BaseModel):
    attempt_id: int
    proposal_id: int
    status: str
    confirmation_token: str
    confirmation_phrase: str
    expires_at: datetime
    planned_publish_at: str
    caption: str
    local_image_path: str
    channel_name: str


class AssistedPost(BaseModel):
    proposal_id: int
    planned_publish_at: str
    caption: str
    image_url: str
    rights_status: str
    warnings: list[str] = Field(default_factory=list)


class AssistedPreparation(BaseModel):
    mode: Literal["assisted"] = "assisted"
    channel_name: str
    channel_id: str
    timezone: str
    youtube_url: str
    items: list[AssistedPost]


class VerificationResult(BaseModel):
    proposal_id: int
    verified: bool
    status: str
    detail: str


class PostPublisher(Protocol):
    async def validate_session(self) -> PublisherSessionStatus: ...

    async def prepare_post(self, proposal_id: int) -> PreparedPost: ...

    async def schedule_post(self, proposal_id: int) -> PublishResult: ...

    async def verify_scheduled_post(self, proposal_id: int) -> VerificationResult: ...
