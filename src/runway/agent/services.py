"""The post and media service seam the MCP tools and the approval queue call.

The publishing engine (posts, targets, media) is built on another branch. The
agent layer is written against these Protocols; the engine's real functions
are connected through one call, `register_agent_services`, after both
branches merge. Until then the defaults answer 409 `capability_not_ready`,
and the approval queue falls back to a guarded read of `draft_targets`.

Contract for an implementation:

- Every method receives the acting `Principal` and enforces workspace
  isolation itself. Scope checks are done by the caller (MCP tool or route)
  and may be repeated.
- Errors are raised as `runway.auth.problems.Problem` (`not_found`,
  `version_conflict`, `validation_failed`, `capability_not_ready`, ...).
- Results are JSON-ready dicts. Text that came from a platform (an error a
  platform returned, a remote title) must sit under the keys listed in
  `PLATFORM_TEXT_KEYS`, so the MCP layer can wrap it as untrusted.
- A post created by an agent principal gets `source='agent'`; the tool layer
  already defaults `ai_generated` to true for agents.
- `request_release` never releases: it marks the target as waiting for a
  person and writes `target.awaiting_release`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from runway.auth.principal import Principal
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database

JSONObject = dict[str, Any]

# Keys whose values may hold platform-authored text in post, target and media
# views; the MCP layer wraps them in {"untrusted": true, ...}.
PLATFORM_TEXT_KEYS = frozenset(
    {"error", "last_error", "error_detail", "state_reason", "platform_message", "remote_title"}
)


class TargetDraft(BaseModel):
    account_id: int = Field(ge=1)
    post_type: str | None = Field(default=None, max_length=40)
    caption_override: str | None = Field(default=None, max_length=8000)
    settings: dict[str, Any] = Field(default_factory=dict)


class PostDraft(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20000)
    media_ids: list[int] = Field(default_factory=list, max_length=20)
    targets: list[TargetDraft] = Field(default_factory=list, max_length=20)
    # None: true for agent principals (spec 06 section 4), false otherwise.
    ai_generated: bool | None = None
    source_ref: str | None = Field(default=None, max_length=200)


class PostChanges(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=20000)
    media_ids: list[int] | None = Field(default=None, max_length=20)
    ai_generated: bool | None = None


class TargetSettings(BaseModel):
    post_type: str | None = Field(default=None, max_length=40)
    caption_override: str | None = Field(default=None, max_length=8000)
    settings: dict[str, Any] | None = None


class ApprovalItem(BaseModel):
    """A target an agent prepared that waits for a person to release or reject it."""

    target_id: int
    post_id: int
    platform: str
    account_id: int | None
    post_type: str | None = None
    title: str
    planned_at: datetime | None = None
    payload_hash: str | None = None
    version: int | None = None
    source: str | None = None
    created_by: str | None = None
    ai_generated: bool | None = None


class ApprovalPage(BaseModel):
    items: list[ApprovalItem]
    next_cursor: str | None


class PostService(Protocol):
    def create_post(self, principal: Principal, draft: PostDraft) -> JSONObject: ...

    def update_post(
        self, principal: Principal, post_id: int, version: int, changes: PostChanges
    ) -> JSONObject: ...

    def set_target_settings(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        version: int,
        settings: TargetSettings,
    ) -> JSONObject: ...

    def validate_post(self, principal: Principal, post_id: int) -> JSONObject: ...

    def schedule_target(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        version: int,
        planned_at: datetime | None,
    ) -> JSONObject:
        """Set the planned time; `None` means as soon as released. Never releases."""
        ...

    def cancel_target(self, principal: Principal, post_id: int, target_id: int) -> JSONObject: ...

    def list_posts(self, principal: Principal, cursor: str | None, limit: int) -> JSONObject:
        """`{"items": [...], "next_cursor": str | None}`."""
        ...

    def get_post(self, principal: Principal, post_id: int) -> JSONObject: ...

    def target_status(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        """Must include `platform`, `state` and `payload_hash`."""
        ...

    def request_release(self, principal: Principal, post_id: int, target_id: int) -> JSONObject: ...

    def release_target(
        self, principal: Principal, post_id: int, target_id: int, payload_hash: str
    ) -> JSONObject: ...

    def reject_target(self, principal: Principal, target_id: int, reason: str) -> None:
        """Return an agent's target to `draft` after a person rejected it."""
        ...

    def list_awaiting_release(
        self, workspace_id: int, cursor: str | None, limit: int
    ) -> ApprovalPage: ...


class MediaService(Protocol):
    def upload_from_path(self, principal: Principal, path: Path) -> JSONObject:
        """Start an upload of a local file; returns the media id at once (status `processing`)."""
        ...

    def get_media(self, principal: Principal, media_id: int) -> JSONObject: ...


def engine_not_connected() -> Problem:
    return Problem.of(
        ProblemCode.CAPABILITY_NOT_READY,
        "the publishing engine is not connected in this build",
    )


class UnconnectedPostService:
    """Default until the engine registers itself; every call is `capability_not_ready`."""

    def create_post(self, principal: Principal, draft: PostDraft) -> JSONObject:
        raise engine_not_connected()

    def update_post(
        self, principal: Principal, post_id: int, version: int, changes: PostChanges
    ) -> JSONObject:
        raise engine_not_connected()

    def set_target_settings(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        version: int,
        settings: TargetSettings,
    ) -> JSONObject:
        raise engine_not_connected()

    def validate_post(self, principal: Principal, post_id: int) -> JSONObject:
        raise engine_not_connected()

    def schedule_target(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        version: int,
        planned_at: datetime | None,
    ) -> JSONObject:
        raise engine_not_connected()

    def cancel_target(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        raise engine_not_connected()

    def list_posts(self, principal: Principal, cursor: str | None, limit: int) -> JSONObject:
        raise engine_not_connected()

    def get_post(self, principal: Principal, post_id: int) -> JSONObject:
        raise engine_not_connected()

    def target_status(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        raise engine_not_connected()

    def request_release(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        raise engine_not_connected()

    def release_target(
        self, principal: Principal, post_id: int, target_id: int, payload_hash: str
    ) -> JSONObject:
        raise engine_not_connected()

    def reject_target(self, principal: Principal, target_id: int, reason: str) -> None:
        # Nothing to move back before the engine exists; the event is still written.
        return None

    def list_awaiting_release(
        self, workspace_id: int, cursor: str | None, limit: int
    ) -> ApprovalPage:
        raise engine_not_connected()


class UnconnectedMediaService:
    def upload_from_path(self, principal: Principal, path: Path) -> JSONObject:
        raise engine_not_connected()

    def get_media(self, principal: Principal, media_id: int) -> JSONObject:
        raise engine_not_connected()


@dataclass(frozen=True, slots=True)
class AgentServices:
    posts: PostService
    media: MediaService
    # False while the defaults above are in place: the approval queue then
    # reads draft_targets directly (see runway.agent.approvals).
    connected: bool = False


AgentServicesFactory = Callable[[Database, Settings], AgentServices]
_factory: AgentServicesFactory | None = None


def register_agent_services(factory: AgentServicesFactory | None) -> None:
    """Connect the publishing engine: one call, made where the app and MCP server start.

    `factory(database, settings)` returns `AgentServices(posts=..., media=...,
    connected=True)`. Passing None restores the unconnected defaults.
    """
    global _factory
    _factory = factory


def build_agent_services(database: Database, settings: Settings) -> AgentServices:
    if _factory is not None:
        return _factory(database, settings)
    return AgentServices(posts=UnconnectedPostService(), media=UnconnectedMediaService())
