"""ASGI middleware: request context + access log, security headers, the proxy guard, CORS,
compression, body size limit.

Pure ASGI classes (not ``BaseHTTPMiddleware``) so they add no task overhead and work with
streaming responses.
"""

from __future__ import annotations

import hmac
import re
import time
import uuid

import structlog
from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import REQUEST_ID_HEADER, request_id_var
from app.core.errors import ErrorCode, ErrorResponse
from app.core.idempotency import REPLAYED_HEADER
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
            # user_id is bound later by the auth dependency; None here resets it per request.
            with structlog.contextvars.bound_contextvars(request_id=request_id, user_id=None):
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


class ProxyGuardMiddleware:
    """Answers only requests that carry the /api proxy's shared secret (ADR-0019), but those
    to ``exempt_paths``: nobody reaches the API around the proxy, so the client address the
    proxy passes on (X-Forwarded-For) is the real one. The rest get a 403, read no further."""

    def __init__(
        self, app: ASGIApp, *, secret: str, header: str, exempt_paths: tuple[str, ...]
    ) -> None:
        self.app = app
        self.secret = secret.encode()
        self.header = header
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt_paths or self._through(scope):
            await self.app(scope, receive, send)
            return
        request_id = scope.get("state", {}).get("request_id")
        body = ErrorResponse(
            code=ErrorCode.FORBIDDEN,
            message="This API answers only through the app.",
            request_id=request_id,
        )
        await JSONResponse(body.model_dump(), status_code=403)(scope, receive, send)

    def _through(self, scope: Scope) -> bool:
        given = Headers(scope=scope).get(self.header, "").encode()
        return hmac.compare_digest(given, self.secret)


class CompressionMiddleware:
    """Gzips answers of ``minimum_size`` bytes or more for callers that accept it (the free host
    counts every byte the API sends), but never those under ``skip_prefixes``: an answer that
    holds a token is never compressed next to what the caller sent (BREACH)."""

    def __init__(self, app: ASGIApp, *, minimum_size: int, skip_prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=6)
        self.skip_prefixes = skip_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not scope["path"].startswith(self.skip_prefixes):
            await self.gzip(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """Refuses a request body larger than ``max_bytes`` (413, the usual error envelope) before
    the app reads any of it: by its Content-Length, or, for a body sent in chunks without one,
    by counting it as it arrives. Such a body is held here (never more than ``max_bytes``) and
    handed on whole: an error raised while FastAPI reads a body would come out as a 400."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        length = headers.get("content-length")
        if length is not None or "chunked" not in headers.get("transfer-encoding", "").lower():
            # The server holds a body to its Content-Length, so the header is enough.
            if length is not None and length.isdigit() and int(length) > self.max_bytes:
                await self._refuse(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        held: list[Message] = []
        size = 0
        while True:
            message = await receive()
            held.append(message)
            if message["type"] != "http.request":
                break  # the client went away: the app hears it as it would
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await self._refuse(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return held.pop(0) if held else await receive()

        await self.app(scope, replay, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = scope.get("state", {}).get("request_id")
        body = ErrorResponse(
            code=ErrorCode.REQUEST_TOO_LARGE,
            message="That request is too large.",
            details={"max_bytes": self.max_bytes},
            request_id=request_id,
        )
        response = JSONResponse(body.model_dump(), status_code=413)
        await response(scope, receive, send)


def install_middleware(app: FastAPI, settings: Settings) -> None:
    """Register middleware. Starlette runs the last-added one outermost, so the order below
    is body limit (innermost) -> compression -> CORS -> proxy guard -> security headers ->
    request context (outermost): a refused request still gets security headers, its request id
    and its log line, and one from around the proxy is refused before anything else looks at
    it."""
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.api.max_body_bytes)
    if settings.api.gzip_min_bytes > 0:
        app.add_middleware(
            CompressionMiddleware,
            minimum_size=settings.api.gzip_min_bytes,
            skip_prefixes=settings.api.gzip_skip_paths,
        )
    if settings.cors.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
            # A client tells a replayed create (a retry after a lost reply) by its header, and
            # names a downloaded file (export.csv) as the server did.
            expose_headers=[REQUEST_ID_HEADER, REPLAYED_HEADER, "Content-Disposition"],
        )
    security = settings.security
    if security.proxy_secret is not None:
        app.add_middleware(
            ProxyGuardMiddleware,
            secret=security.proxy_secret.get_secret_value(),
            header=security.proxy_header,
            exempt_paths=security.proxy_exempt_paths,
        )
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_max_age_seconds=settings.security.hsts_max_age_seconds,
    )
    app.add_middleware(RequestContextMiddleware, quiet_paths=settings.log.quiet_paths)
