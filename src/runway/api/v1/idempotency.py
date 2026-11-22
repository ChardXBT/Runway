"""`Idempotency-Key` for v1 mutations (spec 06 section 5).

A route opts in with `@idempotent`. A request that carries the header is
remembered per workspace, principal and key for 24 hours:

- same key, same request: the stored response is replayed and the handler
  does not run again (`Idempotent-Replayed: true`);
- same key, different request: 409 `idempotency_conflict`;
- same key while the first request is still running: 409
  `idempotency_conflict` with `Retry-After`, never a second execution.

Only successful (2xx) JSON responses are stored. A failed request releases
its key, so the client can correct the request and retry with the same key.
If the process dies after the handler committed but before the response was
stored, the key stays "in progress" until it expires: the safe outcome is a
refusal, not a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, TypeVar

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runway.auth.principal import Principal, get_principal
from runway.auth.problems import Problem, ProblemCode
from runway.db.base import Database
from runway.db.models import IdempotencyKey, utcnow

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
IDEMPOTENCY_RETENTION = timedelta(hours=24)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,200}$")
_MARKER = "__runway_idempotent__"

EndpointT = TypeVar("EndpointT", bound=Callable[..., Any])


def idempotent(endpoint: EndpointT) -> EndpointT:
    """Opt a v1 mutation route into Idempotency-Key handling.

    Apply it under the route decorator:

        @router.post("/posts")
        @idempotent
        def create_post(...): ...
    """
    setattr(endpoint, _MARKER, True)
    return endpoint


def is_idempotent(endpoint: Callable[..., Any]) -> bool:
    return bool(getattr(endpoint, _MARKER, False))


def request_fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    """SHA-256 over the request; JSON bodies are canonicalized first."""
    try:
        canonical = json.dumps(
            json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (ValueError, UnicodeDecodeError):
        canonical = body
    digest = hashlib.sha256()
    for part in (method.upper().encode(), path.encode(), query.encode(), canonical):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


class Reservation(Enum):
    RESERVED = "reserved"
    REPLAY = "replay"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class ReserveResult:
    outcome: Reservation
    status: int | None = None
    body: str | None = None


class IdempotencyStore:
    def __init__(self, database: Database, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.database = database
        self.clock = clock

    def _find(self, session: Session, workspace_id: int, principal_id: str, key: str) -> Any:
        return session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.workspace_id == workspace_id,
                IdempotencyKey.principal_id == principal_id,
                IdempotencyKey.key == key,
            )
        )

    def _expired(self, row: IdempotencyKey) -> bool:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=self.clock().tzinfo)
        return created < self.clock() - IDEMPOTENCY_RETENTION

    def reserve(
        self, workspace_id: int, principal_id: str, key: str, request_hash: str
    ) -> ReserveResult:
        try:
            with self.database.session() as session:
                row = self._find(session, workspace_id, principal_id, key)
                if row is not None and self._expired(row):
                    session.delete(row)
                    session.flush()
                    row = None
                if row is None:
                    session.add(
                        IdempotencyKey(
                            workspace_id=workspace_id,
                            principal_id=principal_id,
                            key=key,
                            request_hash=request_hash,
                            created_at=self.clock(),
                        )
                    )
                    session.flush()
                    return ReserveResult(Reservation.RESERVED)
                return self._existing(row, request_hash)
        except IntegrityError:
            # A concurrent request reserved the same key first.
            with self.database.session() as session:
                row = self._find(session, workspace_id, principal_id, key)
                if row is None:
                    raise
                return self._existing(row, request_hash)

    @staticmethod
    def _existing(row: IdempotencyKey, request_hash: str) -> ReserveResult:
        if row.request_hash != request_hash:
            return ReserveResult(Reservation.CONFLICT)
        if row.response_status is None:
            return ReserveResult(Reservation.IN_PROGRESS)
        return ReserveResult(Reservation.REPLAY, row.response_status, row.response_body)

    def complete(
        self, workspace_id: int, principal_id: str, key: str, status: int, body: str
    ) -> None:
        with self.database.session() as session:
            row = self._find(session, workspace_id, principal_id, key)
            if row is not None:
                row.response_status = status
                row.response_body = body

    def release(self, workspace_id: int, principal_id: str, key: str) -> None:
        with self.database.session() as session:
            row = self._find(session, workspace_id, principal_id, key)
            if row is not None and row.response_status is None:
                session.delete(row)

    def purge_expired(self, session: Session) -> int:
        result = session.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.created_at < self.clock() - IDEMPOTENCY_RETENTION
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


def _validated_key(raw: str) -> str:
    key = raw.strip()
    if not _KEY_PATTERN.match(key):
        raise Problem.of(
            ProblemCode.VALIDATION_FAILED,
            f"{IDEMPOTENCY_HEADER} must be 1-200 printable ASCII characters without spaces",
        )
    return key


RouteHandler = Callable[[Request], Coroutine[Any, Any, Response]]


def wrap_idempotent_handler(handler: RouteHandler) -> RouteHandler:
    """Wrap an APIRoute request handler with the Idempotency-Key protocol."""

    async def idempotent_handler(request: Request) -> Response:
        raw_key = request.headers.get(IDEMPOTENCY_HEADER)
        if raw_key is None or request.method not in MUTATING_METHODS:
            return await handler(request)
        key = _validated_key(raw_key)
        # The principal scopes the key; the route's own dependency resolves it again.
        principal: Principal = get_principal(request)
        database = request.app.state.database
        if not isinstance(database, Database):
            raise RuntimeError("the application has no database; build it with create_app()")
        store = IdempotencyStore(database)
        body = await request.body()  # cached on the request for the handler
        fingerprint = request_fingerprint(request.method, request.url.path, request.url.query, body)
        scope = (principal.workspace_id, f"{principal.kind}:{principal.id}", key)
        reserved = store.reserve(*scope, fingerprint)
        if reserved.outcome is Reservation.CONFLICT:
            raise Problem.of(
                ProblemCode.IDEMPOTENCY_CONFLICT,
                "this Idempotency-Key was already used with a different request",
            )
        if reserved.outcome is Reservation.IN_PROGRESS:
            raise Problem.of(
                ProblemCode.IDEMPOTENCY_CONFLICT,
                "a request with this Idempotency-Key is still being processed",
                headers={"Retry-After": "1"},
            )
        if reserved.outcome is Reservation.REPLAY:
            return Response(
                content=reserved.body or "",
                status_code=reserved.status or 200,
                media_type="application/json",
                headers={REPLAYED_HEADER: "true"},
            )
        try:
            response = await handler(request)
        except BaseException:
            store.release(*scope)
            raise
        stored_body = getattr(response, "body", None)
        if 200 <= response.status_code < 300 and isinstance(stored_body, bytes):
            store.complete(*scope, response.status_code, stored_body.decode("utf-8"))
        else:
            store.release(*scope)
        return response

    return idempotent_handler
