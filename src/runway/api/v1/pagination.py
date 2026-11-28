"""Cursor pagination for v1 lists: `?cursor=…&limit=…` -> `next_cursor`.

Cursors are opaque to clients (base64url JSON). They are keyset positions,
bound to one list (`scope`) and one ordering, so a cursor from one list or
order cannot be replayed against another. The query still filters by the
principal's workspace, so a forged cursor can only skip rows, never reveal any.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select
from sqlalchemy.orm import InstrumentedAttribute, Session

from runway.auth.problems import Problem, ProblemCode

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_CURSOR_VERSION = 1

ItemT = TypeVar("ItemT")
RowT = TypeVar("RowT")


class CursorPage(BaseModel, Generic[ItemT]):
    """Response shape of every v1 list."""

    items: list[ItemT]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CursorParams:
    cursor: str | None
    limit: int


def cursor_params(
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> CursorParams:
    """FastAPI dependency: `params: CursorParams = Depends(cursor_params)`."""
    return CursorParams(cursor=cursor or None, limit=limit)


def _invalid_cursor() -> Problem:
    return Problem.of(
        ProblemCode.VALIDATION_FAILED,
        "the cursor is not valid for this list; start again without one",
    )


def encode_cursor(position: int, *, scope: str, descending: bool) -> str:
    raw = json.dumps(
        {"v": _CURSOR_VERSION, "s": scope, "d": descending, "k": position},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, scope: str, descending: bool) -> int:
    """The keyset position in `cursor`; a 422 `validation_failed` problem if it is not ours."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload: Any = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error):
        raise _invalid_cursor() from None
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or payload.get("s") != scope
        or payload.get("d") is not descending
        or not isinstance(payload.get("k"), int)
        or isinstance(payload.get("k"), bool)
    ):
        raise _invalid_cursor()
    return int(payload["k"])


def paginate(
    session: Session,
    statement: Select[tuple[RowT]],
    key: InstrumentedAttribute[int],
    params: CursorParams,
    *,
    scope: str,
    descending: bool = True,
) -> tuple[list[RowT], str | None]:
    """One page of `statement` ordered by the unique integer column `key`.

    `statement` must already carry the workspace filter. Returns the rows and
    the cursor for the next page (None on the last page).
    """
    if params.cursor is not None:
        position = decode_cursor(params.cursor, scope=scope, descending=descending)
        statement = statement.where(key < position if descending else key > position)
    statement = statement.order_by(key.desc() if descending else key.asc()).limit(params.limit + 1)
    rows = list(session.scalars(statement).all())
    if len(rows) <= params.limit:
        return rows, None
    rows = rows[: params.limit]
    last = getattr(rows[-1], key.key)
    return rows, encode_cursor(int(last), scope=scope, descending=descending)
