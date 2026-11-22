"""RFC 9457 problem responses with a stable machine-readable `code`."""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(HTTPException):
    """An HTTP error rendered as application/problem+json.

    Subclassing HTTPException keeps the status code correct even in an app
    that has not registered `problem_handler`.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.title = title

    def body(self) -> dict[str, object]:
        return {
            "type": f"https://runway.local/problems/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "code": self.code,
        }


async def problem_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, Problem):  # pragma: no cover - registered for Problem only
        raise exc
    return JSONResponse(
        exc.body(),
        status_code=exc.status_code,
        headers=exc.headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )
