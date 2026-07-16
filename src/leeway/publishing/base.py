from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class PublisherSessionStatus(BaseModel):
    valid: bool
    publisher: str
    detail: str


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
