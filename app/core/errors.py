"""One error envelope for every failure: ``{code, message, details, request_id}``.

Services raise :class:`AppError` subclasses (never ``HTTPException``; ruff bans it). The
handlers registered here turn those, request-validation errors, framework HTTP errors and
unexpected exceptions into the same JSON shape, so clients handle errors in one place and
translate them by ``code``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import REQUEST_ID_HEADER, current_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"
    SERVICE_NOT_READY = "SERVICE_NOT_READY"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorResponse(BaseModel):
    """Body of every non-2xx response."""

    code: str = Field(examples=[ErrorCode.NOT_FOUND])
    message: str = Field(examples=["Resource not found."])
    details: Any = None
    request_id: str | None = Field(default=None, examples=["5f0c3e7a9b2d4c1e8f6a0b3c2d1e4f5a"])


class AppError(Exception):
    """Base class for expected, client-facing errors."""

    status_code: ClassVar[int] = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR
    default_message: ClassVar[str] = "Internal server error."

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND
    default_message = "Resource not found."


class ServiceNotReadyError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.SERVICE_NOT_READY
    default_message = "Service is not ready yet. Please retry shortly."


# Used as the app-wide default so OpenAPI documents the envelope instead of FastAPI's
# HTTPValidationError, and the generated frontend types match what the API really returns.
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse, "description": "Invalid input"},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse, "description": "Server error"},
}

_HTTP_STATUS_CODES: dict[int, ErrorCode] = {
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
}


def _request_id(request: Request) -> str | None:
    # Unhandled errors are rendered outside the request-context middleware, after its
    # contextvar is reset, so the id is also kept on request.state.
    return getattr(request.state, "request_id", None) or current_request_id()


def error_json(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body = ErrorResponse(code=code, message=message, details=details, request_id=request_id)
    response_headers = dict(headers or {})
    if request_id:
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(body.model_dump(), status_code=status_code, headers=response_headers)


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    return error_json(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Never echo submitted values back (they may be passwords); keep location, message, type.
    details = [
        {"loc": list(error.get("loc", ())), "msg": error.get("msg", ""), "type": error.get("type")}
        for error in exc.errors()
    ]
    return error_json(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code=ErrorCode.VALIDATION_ERROR,
        message="The request is invalid.",
        details=details,
    )


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code = _HTTP_STATUS_CODES.get(exc.status_code, ErrorCode.HTTP_ERROR)
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_json(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_error",
        request_id=_request_id(request),
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return error_json(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=ErrorCode.INTERNAL_ERROR,
        message=AppError.default_message,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
