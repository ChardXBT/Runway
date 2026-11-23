"""`/api/v1/posts`: posts, targets, release and the attempt ledger (spec 03 section 8).

Thin HTTP layer over `runway.posts.service.PostService`; every rule lives in
the service so the agent interface gets exactly the same behaviour.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response, status

from runway.api.v1.concurrency import etag_for, if_match_version, parse_if_match
from runway.api.v1.idempotency import idempotent
from runway.api.v1.pagination import CursorPage, CursorParams, cursor_params
from runway.api.v1.router import build_v1_router
from runway.auth.principal import Principal, get_principal
from runway.posts.schemas import (
    AttemptOut,
    MarkPublishedRequest,
    PostCreate,
    PostOut,
    PostPatch,
    ReconcileRequest,
    ReleaseRequest,
    ScheduleRequest,
    TargetInput,
    TargetUpdate,
)
from runway.posts.service import PostService


def _with_etag(response: Response, post: PostOut) -> PostOut:
    response.headers["ETag"] = etag_for(post.version)
    return post


def build_posts_router(service: PostService) -> APIRouter:
    router = build_v1_router(tags=["posts"], prefix="/posts")

    @router.post("", status_code=status.HTTP_201_CREATED)
    @idempotent
    def create_post(
        payload: PostCreate,
        response: Response,
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Create a post with its media and targets (`posts:write`). Nothing is released."""
        return _with_etag(response, service.create_post(principal, payload))

    @router.get("")
    def list_posts(
        params: CursorParams = Depends(cursor_params),
        principal: Principal = Depends(get_principal),
    ) -> CursorPage[PostOut]:
        """Posts of the workspace, newest first (`posts:read`)."""
        items, next_cursor = service.list_posts(principal, params)
        return CursorPage[PostOut](items=items, next_cursor=next_cursor)

    @router.get("/{post_id}")
    def get_post(
        post_id: int, response: Response, principal: Principal = Depends(get_principal)
    ) -> PostOut:
        """One post with its targets, validation issues and capability per target."""
        return _with_etag(response, service.get_post(principal, post_id))

    @router.patch("/{post_id}")
    @idempotent
    def update_post(
        post_id: int,
        payload: PostPatch,
        response: Response,
        expected: int = Depends(if_match_version),
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Edit the post (`If-Match: "<version>"`). Released targets return to ready."""
        return _with_etag(response, service.update_post(principal, post_id, expected, payload))

    @router.post("/{post_id}/targets", status_code=status.HTTP_201_CREATED)
    @idempotent
    def add_target(
        post_id: int,
        payload: TargetInput,
        response: Response,
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Add one more account to the post (`posts:write`)."""
        return _with_etag(response, service.add_target(principal, post_id, payload))

    @router.put("/{post_id}/targets/{target_id}")
    @idempotent
    def update_target(
        post_id: int,
        target_id: int,
        payload: TargetUpdate,
        response: Response,
        expected: int = Depends(if_match_version),
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Settings, caption override and post type (`If-Match`: the target's version)."""
        return _with_etag(
            response, service.update_target(principal, post_id, target_id, expected, payload)
        )

    @router.post("/{post_id}/targets/{target_id}/schedule")
    @idempotent
    def schedule_target(
        post_id: int,
        target_id: int,
        payload: ScheduleRequest,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Set `planned_at` (or `now: true`) (`posts:schedule`). Clears a release."""
        expected = parse_if_match(if_match) if if_match is not None else None
        return _with_etag(
            response,
            service.schedule_target(
                principal, post_id, target_id, payload, expected_version=expected
            ),
        )

    @router.post("/{post_id}/targets/{target_id}/release")
    @idempotent
    def release_target(
        post_id: int,
        target_id: int,
        payload: ReleaseRequest,
        response: Response,
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Approve this exact payload (`posts:release`): echo `payload_hash`, `confirmed: true`.

        TikTok targets are refused for API keys (403 `human_required`).
        """
        return _with_etag(response, service.release_target(principal, post_id, target_id, payload))

    @router.post("/{post_id}/targets/{target_id}/cancel")
    @idempotent
    def cancel_target(
        post_id: int,
        target_id: int,
        response: Response,
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Withdraw a target before it is sent (`posts:schedule`)."""
        return _with_etag(response, service.cancel_target(principal, post_id, target_id))

    @router.post("/{post_id}/targets/{target_id}/reconcile")
    @idempotent
    def reconcile_target(
        post_id: int,
        target_id: int,
        response: Response,
        payload: ReconcileRequest | None = None,
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """Ask the platform again about an `uncertain` target, or record a person's finding."""
        return _with_etag(
            response,
            service.reconcile_target(principal, post_id, target_id, payload or ReconcileRequest()),
        )

    @router.post("/{post_id}/targets/{target_id}/mark-published")
    @idempotent
    def mark_published(
        post_id: int,
        target_id: int,
        payload: MarkPublishedRequest,
        response: Response,
        principal: Principal = Depends(get_principal),
    ) -> PostOut:
        """The assisted adapter's "mark as published", and manual resolution (`posts:release`)."""
        return _with_etag(response, service.mark_published(principal, post_id, target_id, payload))

    @router.get("/{post_id}/targets/{target_id}/attempts")
    def list_attempts(
        post_id: int,
        target_id: int,
        params: CursorParams = Depends(cursor_params),
        principal: Principal = Depends(get_principal),
    ) -> CursorPage[AttemptOut]:
        """The target's append-only attempt ledger, newest first (`posts:read`)."""
        items, next_cursor = service.list_attempts(principal, post_id, target_id, params)
        return CursorPage[AttemptOut](items=items, next_cursor=next_cursor)

    return router
