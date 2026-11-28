"""The publishing engine behind the agent seam (`runway.agent.services`).

`EnginePostService` and `EngineMediaService` implement the agent layer's
`PostService` / `MediaService` Protocols by calling the engine's real services
(`runway.posts.service.PostService`, `runway.posts.media_service.MediaService`),
so the MCP tools and the approval queue enforce exactly what `/api/v1/posts`
and `/api/v1/media` enforce: workspace isolation, scopes, versions, explicit
privacy, TikTok `human_required` and the release rules.

`engine_agent_services` is the default factory of `build_agent_services`; tests
may still register a fake with `register_agent_services`.
"""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from runway.agent.services import (
    AgentServices,
    ApprovalItem,
    ApprovalPage,
    JSONObject,
    PostChanges,
    PostDraft,
    TargetSettings,
)
from runway.api.v1.pagination import CursorParams, decode_cursor, encode_cursor
from runway.auth.principal import Principal
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount, ContentDraft, DraftTarget
from runway.domain.enums import TargetState
from runway.mcp.untrusted import untrusted_text
from runway.platforms.capabilities import Platform
from runway.posts import media_service as engine_media
from runway.posts import service as engine_posts
from runway.posts.jobs import cancel_pending_jobs
from runway.posts.schemas import (
    PostCreate,
    PostOut,
    PostPatch,
    ReleaseRequest,
    ScheduleRequest,
    TargetInput,
    TargetOut,
    TargetUpdate,
)
from runway.posts.snapshot import snapshot
from runway.posts.states import current_state, set_target_state
from runway.posts.types import PostSource

APPROVALS_CURSOR_SCOPE = "approvals"
REJECTED_REASON = "rejected by a person; see the target.rejected event"
# A person can reject a target that waits for them (or one still being prepared).
REJECTABLE_STATES = frozenset({TargetState.READY, TargetState.DRAFT})


def _problem(code: ProblemCode, detail: str, **extra: Any) -> Problem:
    return Problem.of(code, detail, extra=extra or None)


def _agent_target(target: TargetOut) -> JSONObject:
    """A target as the agent sees it: the platform's raw response is untrusted text."""
    data: JSONObject = target.model_dump(mode="json")
    attempt = data.get("last_attempt")
    if isinstance(attempt, dict) and attempt.get("response"):
        attempt["response"] = untrusted_text(
            json.dumps(attempt["response"], sort_keys=True, default=str)
        )
    return data


def _agent_post(post: PostOut) -> JSONObject:
    data: JSONObject = post.model_dump(mode="json")
    data["post_id"] = post.id
    data["targets"] = [_agent_target(target) for target in post.targets]
    return data


def _find_target(post: PostOut, target_id: int) -> TargetOut:
    for target in post.targets:
        if target.id == target_id:
            return target
    raise _problem(ProblemCode.NOT_FOUND, "this target does not exist")


def _target_result(post: PostOut, target_id: int) -> JSONObject:
    target = _find_target(post, target_id)
    view = _agent_target(target)
    return {
        **view,
        "post_id": post.id,
        "target_id": target.id,
        "post_version": post.version,
        "awaiting_release": target.state == TargetState.READY.value
        and post.source == PostSource.AGENT.value,
    }


class EnginePostService:
    """The agent `PostService` Protocol over `runway.posts.service.PostService`."""

    def __init__(self, posts: engine_posts.PostService) -> None:
        self.posts = posts
        self.database = posts.database

    # -------------------------------------------------------------- writing

    def _platform(self, principal: Principal, account_id: int) -> Platform:
        with self.database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None or account.workspace_id != principal.workspace_id:
                raise _problem(ProblemCode.NOT_FOUND, "this account does not exist")
            return Platform(account.platform)

    def create_post(self, principal: Principal, draft: PostDraft) -> JSONObject:
        data = PostCreate(
            title=draft.title,
            body=draft.body,
            media_ids=draft.media_ids,
            targets=[
                TargetInput(
                    platform=self._platform(principal, target.account_id),
                    account_id=target.account_id,
                    post_type=target.post_type,
                    caption_override=target.caption_override,
                    settings=target.settings,
                )
                for target in draft.targets
            ],
            ai_generated=draft.ai_generated,
        )
        with self.database.session() as session:
            post = self.posts.create_post_in(session, principal, data, source_ref=draft.source_ref)
            view = engine_posts.post_view(session, self.posts.sources, post)
        return _agent_post(view)

    def update_post(
        self, principal: Principal, post_id: int, version: int, changes: PostChanges
    ) -> JSONObject:
        patch = PostPatch(
            title=changes.title,
            body=changes.body,
            media_ids=changes.media_ids,
            ai_generated=changes.ai_generated,
        )
        return _agent_post(self.posts.update_post(principal, post_id, version, patch))

    def set_target_settings(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        version: int,
        settings: TargetSettings,
    ) -> JSONObject:
        # The engine replaces the target's whole editable state; fields left
        # out by the agent keep their current values.
        current = _find_target(self.posts.get_post(principal, post_id), target_id)
        update = TargetUpdate(
            post_type=settings.post_type if settings.post_type is not None else current.post_type,
            account_id=current.account_id,
            caption_override=(
                settings.caption_override
                if settings.caption_override is not None
                else current.caption_override
            ),
            settings=settings.settings if settings.settings is not None else current.settings,
        )
        post = self.posts.update_target(principal, post_id, target_id, version, update)
        return _target_result(post, target_id)

    def validate_post(self, principal: Principal, post_id: int) -> JSONObject:
        post = self.posts.get_post(principal, post_id)
        targets = [
            {
                "target_id": target.id,
                "platform": target.platform,
                "state": target.state,
                "releasable": target.releasable,
                "issues": [issue.model_dump(mode="json") for issue in target.issues],
            }
            for target in post.targets
        ]
        return {
            "post_id": post.id,
            "valid": all(
                not any(issue.severity == "error" for issue in target.issues)
                for target in post.targets
            ),
            "targets": targets,
        }

    def schedule_target(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        version: int,
        planned_at: datetime | None,
    ) -> JSONObject:
        request = (
            ScheduleRequest(now=True)
            if planned_at is None
            else ScheduleRequest(planned_at=planned_at)
        )
        post = self.posts.schedule_target(
            principal, post_id, target_id, request, expected_version=version
        )
        return _target_result(post, target_id)

    def cancel_target(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        return _target_result(self.posts.cancel_target(principal, post_id, target_id), target_id)

    # -------------------------------------------------------------- reading

    def list_posts(self, principal: Principal, cursor: str | None, limit: int) -> JSONObject:
        items, next_cursor = self.posts.list_posts(
            principal, CursorParams(cursor=cursor, limit=limit)
        )
        return {"items": [_agent_post(post) for post in items], "next_cursor": next_cursor}

    def get_post(self, principal: Principal, post_id: int) -> JSONObject:
        return _agent_post(self.posts.get_post(principal, post_id))

    def target_status(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        return _target_result(self.posts.get_post(principal, post_id), target_id)

    # -------------------------------------------------------------- release

    def request_release(self, principal: Principal, post_id: int, target_id: int) -> JSONObject:
        """Never releases. A `ready` target of an agent's post is already in the queue.

        The engine writes `target.awaiting_release` when an agent's target
        becomes `ready`; this answers whether the target is waiting for a
        person now and, if not, which issues keep it out of the queue.
        """
        post = self.posts.get_post(principal, post_id)
        result = _target_result(post, target_id)
        target = _find_target(post, target_id)
        blocking = [issue.model_dump(mode="json") for issue in target.issues]
        result["released"] = False
        if result["awaiting_release"]:
            result["detail"] = "waiting for a person to release or reject it in Runway"
        elif post.source != PostSource.AGENT.value:
            result["detail"] = "only posts an agent created enter the approval queue"
        else:
            result["detail"] = "fix the issues first; the target is not ready"
            result["blocking_issues"] = blocking
        return result

    def release_target(
        self, principal: Principal, post_id: int, target_id: int, payload_hash: str
    ) -> JSONObject:
        request = ReleaseRequest(payload_hash=payload_hash, confirmed=True)
        post = self.posts.release_target(principal, post_id, target_id, request)
        return _target_result(post, target_id)

    def reject_target(self, principal: Principal, target_id: int, reason: str) -> None:
        """Take the target out of the queue: `cancelled`, with no release and no job.

        `cancelled` is stable (a revalidation would bring a `draft` back to
        `ready`); the agent revives it by changing its settings or schedule.
        """
        with self.database.session() as session:
            target = session.get(DraftTarget, target_id)
            if target is None or target.workspace_id != principal.workspace_id:
                raise _problem(ProblemCode.NOT_FOUND, "no such target in this workspace")
            state = current_state(target)
            if state not in REJECTABLE_STATES:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    f"this target is {state.value}; only a target waiting for release can be "
                    "rejected",
                    state=state.value,
                )
            post = session.get(ContentDraft, target.draft_id)
            cancel_pending_jobs(session, target.id)
            engine_posts.clear_release(target)
            set_target_state(
                session, target, TargetState.CANCELLED, reason=REJECTED_REASON, post=post
            )

    def list_awaiting_release(
        self, workspace_id: int, cursor: str | None, limit: int
    ) -> ApprovalPage:
        """Agent posts' targets in `ready`, newest first, with the exact payload to approve."""
        statement = (
            select(DraftTarget, ContentDraft)
            .join(ContentDraft, DraftTarget.draft_id == ContentDraft.id)
            .where(
                DraftTarget.workspace_id == workspace_id,
                DraftTarget.state == TargetState.READY.value,
                ContentDraft.source == PostSource.AGENT.value,
            )
        )
        if cursor is not None:
            position = decode_cursor(cursor, scope=APPROVALS_CURSOR_SCOPE, descending=True)
            statement = statement.where(DraftTarget.id < position)
        items: list[ApprovalItem] = []
        with self.database.session() as session:
            rows = session.execute(statement.order_by(DraftTarget.id.desc()).limit(limit + 1)).all()
            sources = self.posts.sources
            for target, post in rows[:limit]:
                snap = snapshot(session, sources, target, post=post)
                items.append(
                    ApprovalItem(
                        target_id=target.id,
                        post_id=post.id,
                        platform=target.platform,
                        account_id=target.account_id,
                        post_type=target.post_type,
                        title=post.title,
                        planned_at=target.planned_at,
                        payload_hash=snap.payload_hash,
                        payload=snap.payload,
                        version=target.version,
                        source=post.source,
                        created_by=post.created_by,
                        ai_generated=post.ai_generated,
                    )
                )
        next_cursor = (
            encode_cursor(items[-1].target_id, scope=APPROVALS_CURSOR_SCOPE, descending=True)
            if len(rows) > limit and items
            else None
        )
        return ApprovalPage(items=items, next_cursor=next_cursor)


# Media files `upload_media_from_path` accepts, by suffix (the probe checks the content).
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


class EngineMediaService:
    """The agent `MediaService` Protocol over `runway.posts.media_service.MediaService`."""

    def __init__(self, media: engine_media.MediaService) -> None:
        self.media = media
        self.database = media.database

    def upload_from_path(self, principal: Principal, path: Path) -> JSONObject:
        """Copy the file into the media store; the probe runs as a job (status `processing`)."""
        from runway.auth.principal import MEDIA_WRITE, require

        require(principal, MEDIA_WRITE)
        size = path.stat().st_size
        if size > self.media.settings.media_max_upload_bytes:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                f"files are limited to {self.media.settings.media_max_upload_bytes} bytes",
            )
        content_type = _MEDIA_TYPES.get(path.suffix.lower()) or (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        with self.database.session() as session:
            asset = self.media.import_file(
                session,
                workspace_id=principal.workspace_id,
                source=path,
                content_type=content_type,
                filename=path.name,
                created_by=f"{principal.kind}:{principal.id}",
            )
            view = self.media.view(asset)
        data: JSONObject = view.model_dump(mode="json")
        data["media_id"] = view.id
        return data

    def get_media(self, principal: Principal, media_id: int) -> JSONObject:
        view = self.media.get(principal, media_id)
        data: JSONObject = view.model_dump(mode="json")
        data["media_id"] = view.id
        return data


def engine_agent_services(database: Database, settings: Settings) -> AgentServices:
    """The real engine services, built the way `create_app` builds them."""
    from runway.posts.runtime import build_media_service, build_post_service

    posts = build_post_service(settings, database)
    media = build_media_service(
        settings, database, registry=posts.registry, stores=posts.media_stores
    )
    return AgentServices(
        posts=EnginePostService(posts), media=EngineMediaService(media), connected=True
    )
