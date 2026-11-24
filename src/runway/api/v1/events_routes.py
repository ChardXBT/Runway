"""`GET /api/v1/events/stream`: the workspace's events as server-sent events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from runway.api.v1.router import build_v1_router
from runway.auth.principal import Principal, get_principal
from runway.auth.problems import Problem, ProblemCode
from runway.db.base import Database
from runway.events.stream import HEARTBEAT_SECONDS, POLL_SECONDS, event_stream, wakeup_for
from runway.events.visibility import visibility_for


def _event_id(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not text.isdigit() or len(text) > 18:
        raise Problem.of(ProblemCode.VALIDATION_FAILED, "Last-Event-ID must be an event id")
    return int(text)


def build_events_router(
    database: Database,
    *,
    poll_seconds: float = POLL_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> APIRouter:
    router = build_v1_router(tags=["events"], prefix="/events")

    @router.get(
        "/stream",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def stream_events(
        request: Request,
        principal: Principal = Depends(get_principal),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        last_event_id_query: str | None = Query(
            default=None,
            alias="last_event_id",
            description="For clients that cannot set Last-Event-ID on the first connection.",
        ),
    ) -> StreamingResponse:
        """Events for the caller's workspace, resumable with Last-Event-ID.

        Frames carry `id:` (the event row id), `event:` (the type) and a JSON
        `data:` of ids and a small summary. A `resync` event means the client
        must refetch what it shows. A comment line is sent every 20 seconds
        so proxies keep the connection open.
        """
        resume_from = _event_id(last_event_id if last_event_id is not None else last_event_id_query)
        frames = event_stream(
            database,
            principal.workspace_id,
            resume_from,
            wakeup=wakeup_for(database, poll_seconds=poll_seconds),
            is_disconnected=request.is_disconnected,
            heartbeat_seconds=heartbeat_seconds,
            # Spec 06 section 8: filtered to the scopes the caller may read.
            visible=visibility_for(principal),
        )
        return StreamingResponse(
            frames,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
