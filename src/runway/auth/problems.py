"""RFC 9457 problem responses with a stable machine-readable `code` (spec 06 section 6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemCode(StrEnum):
    """Stable error codes. Clients branch on these, never on `detail` text."""

    SCOPE_MISSING = "scope_missing"
    VERSION_CONFLICT = "version_conflict"
    HUMAN_REQUIRED = "human_required"
    CAPABILITY_NOT_READY = "capability_not_ready"
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RATE_LIMITED = "rate_limited"
    # The item is in a state that does not allow this action (e.g. editing a
    # target while it is being sent).
    INVALID_STATE = "invalid_state"
    # Raised by get_principal (spec 01 section 3).
    AUTHENTICATION_REQUIRED = "authentication_required"
    API_KEY_UNSUPPORTED = "api_key_unsupported"


_DEFAULTS: dict[ProblemCode, tuple[int, str]] = {
    ProblemCode.SCOPE_MISSING: (403, "Scope missing"),
    ProblemCode.VERSION_CONFLICT: (409, "Version conflict"),
    ProblemCode.HUMAN_REQUIRED: (403, "A person must do this"),
    ProblemCode.CAPABILITY_NOT_READY: (409, "Capability not ready"),
    ProblemCode.VALIDATION_FAILED: (422, "Validation failed"),
    ProblemCode.NOT_FOUND: (404, "Not found"),
    ProblemCode.IDEMPOTENCY_CONFLICT: (409, "Idempotency key conflict"),
    ProblemCode.RATE_LIMITED: (429, "Rate limited"),
    ProblemCode.INVALID_STATE: (409, "Not allowed in this state"),
    ProblemCode.AUTHENTICATION_REQUIRED: (401, "Authentication required"),
    ProblemCode.API_KEY_UNSUPPORTED: (401, "API keys are not available yet"),
}


def default_status(code: ProblemCode) -> int:
    return _DEFAULTS[code][0]


class Problem(HTTPException):
    """An HTTP error rendered as application/problem+json.

    Subclassing HTTPException keeps the status code correct even in an app
    that has not registered `problem_handler`. `extra` adds members to the
    body (for example `errors` or `current_version`); it must never carry
    platform text or secrets.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.title = title
        self.extra = dict(extra or {})

    @classmethod
    def of(
        cls,
        code: ProblemCode,
        detail: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Problem:
        """A problem with the code's standard status and title."""
        status, title = _DEFAULTS[code]
        return cls(
            status_code if status_code is not None else status,
            code.value,
            title,
            detail,
            headers=headers,
            extra=extra,
        )

    def body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "type": f"https://runway.local/problems/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "code": self.code,
        }
        for key, value in self.extra.items():
            body.setdefault(key, value)
        return body


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(
        problem.body(),
        status_code=problem.status_code,
        headers=problem.headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def problem_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, Problem):  # pragma: no cover - registered for Problem only
        raise exc
    return problem_response(exc)
