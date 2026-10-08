"""ASGI middleware: request context + access log, security headers, CORS.

Pure ASGI classes (not ``BaseHTTPMiddleware``) so they add no task overhead and work with
streaming responses.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import REQUEST_ID_HEADER, request_id_var
from app.core.logging import get_logger

logger = get_logger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def _resolve_request_id(scope: Scope) -> str:
    incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if incoming and _VALID_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Assigns a request id (or keeps a valid incoming one), echoes it, and logs each request."""

    def __init__(self, app: ASGIApp, *, quiet_paths: tuple[str, ...] = ()) -> None:
        self.app = app
        self.quiet_paths = frozenset(quiet_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # setdefault: error responses may already carry it (see core.errors.error_json).
                MutableHeaders(scope=message).setdefault(REQUEST_ID_HEADER, request_id)
            await send(message)

        token = request_id_var.set(request_id)
        try:
            with structlog.contextvars.bound_contextvars(request_id=request_id):
                try:
                    await self.app(scope, receive, send_with_request_id)
                finally:
                    path: str = scope["path"]
                    log = logger.debug if path in self.quiet_paths else logger.info
                    log(
                        "http_request",
                        method=scope["method"],
                        path=path,
                        status=status_code,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    )
        finally:
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Adds conservative security headers to every HTTP response (unless already set)."""

    def __init__(self, app: ASGIApp, *, hsts_max_age_seconds: int = 0) -> None:
        self.app = app
        self.headers: dict[str, str] = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cache-Control": "no-store",
        }
        if hsts_max_age_seconds > 0:
            self.headers["Strict-Transport-Security"] = (
                f"max-age={hsts_max_age_seconds}; includeSubDomains"
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install_middleware(app: FastAPI, settings: Settings) -> None:
    """Register middleware. Starlette runs the last-added one outermost, so the order below
    is CORS (innermost) -> security headers -> request context (outermost)."""
    if settings.cors.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_max_age_seconds=settings.security.hsts_max_age_seconds,
    )
    app.add_middleware(RequestContextMiddleware, quiet_paths=settings.log.quiet_paths)
