"""Exception handlers: problem+json for /api/v1, the old format everywhere else."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from runway.api.v1.router import V1_PREFIX
from runway.auth.problems import Problem, ProblemCode, problem_handler, problem_response


def is_v1_path(path: str) -> bool:
    return path == V1_PREFIX or path.startswith(V1_PREFIX + "/")


async def _validation_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    if not is_v1_path(request.url.path):
        return await request_validation_exception_handler(request, exc)
    errors = [
        {key: value for key, value in error.items() if key in {"loc", "msg", "type"}}
        for error in exc.errors()
    ]
    return problem_response(
        Problem.of(
            ProblemCode.VALIDATION_FAILED,
            "the request is not valid; see errors",
            extra={"errors": jsonable_encoder(errors)},
        )
    )


async def _http_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if is_v1_path(request.url.path) and exc.status_code == 404:
        return problem_response(Problem.of(ProblemCode.NOT_FOUND, "no such resource"))
    return await http_exception_handler(request, exc)


def install_problem_handlers(application: FastAPI) -> None:
    """`Problem` renders as problem+json anywhere; v1 also converts 422 and 404.

    Validation input is reduced to `loc`, `msg` and `type`, so a rejected
    request body is never echoed back.
    """
    application.add_exception_handler(Problem, problem_handler)
    application.add_exception_handler(RequestValidationError, _validation_handler)
    application.add_exception_handler(StarletteHTTPException, _http_handler)
