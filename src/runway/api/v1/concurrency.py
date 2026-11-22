"""Optimistic concurrency for v1 edits: `If-Match: "<version>"` (spec 03 section 1).

Humans and agents edit the same rows, so an edit states the version it read.
A stale version is a 409 `version_conflict`; a missing header is a 428.
"""

from __future__ import annotations

import re

from fastapi import Header
from sqlalchemy import Table, update
from sqlalchemy.orm import Session

from runway.auth.problems import Problem, ProblemCode

_ENTITY_TAG = re.compile(r'^(?:W/)?"?(\d{1,18})"?$')


def etag_for(version: int) -> str:
    """The `ETag` header value for a row at `version`."""
    return f'"{version}"'


def parse_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise Problem.of(
            ProblemCode.VALIDATION_FAILED,
            'send If-Match with the version you read, for example If-Match: "3"',
            status_code=428,
        )
    match = _ENTITY_TAG.match(value.strip())
    if match is None:
        # `*` and lists are refused: an edit must name exactly one version.
        raise Problem.of(
            ProblemCode.VALIDATION_FAILED,
            'If-Match must be a single version, for example If-Match: "3"',
            status_code=428,
        )
    return int(match.group(1))


def if_match_version(if_match: str | None = Header(default=None, alias="If-Match")) -> int:
    """FastAPI dependency: `expected: int = Depends(if_match_version)`."""
    return parse_if_match(if_match)


def version_conflict(current: int, expected: int) -> Problem:
    return Problem.of(
        ProblemCode.VERSION_CONFLICT,
        f"this item changed since you read it (you sent version {expected}, it is now "
        f"{current}); read it again and reapply your change",
        extra={"current_version": current},
        headers={"ETag": etag_for(current)},
    )


def require_version(current: int, expected: int) -> None:
    if current != expected:
        raise version_conflict(current, expected)


def bump_version(
    session: Session,
    table: Table,
    row_id: int,
    expected: int,
    *,
    version_column: str = "version",
) -> int:
    """Atomically move a row from `expected` to `expected + 1`.

    One compare-and-set UPDATE, so two concurrent editors cannot both pass a
    read-then-check. Raises `version_conflict` (or `not_found`) otherwise.
    """
    column = table.c[version_column]
    result = session.execute(
        update(table)
        .where(table.c.id == row_id, column == expected)
        .values({version_column: expected + 1})
    )
    if int(getattr(result, "rowcount", 0) or 0) == 1:
        return expected + 1
    current = session.execute(
        table.select().with_only_columns(column).where(table.c.id == row_id)
    ).scalar_one_or_none()
    if current is None:
        raise Problem.of(ProblemCode.NOT_FOUND, "this item does not exist")
    raise version_conflict(int(current), expected)
