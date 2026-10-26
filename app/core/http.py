"""Outbound HTTP (Google keys today; free-tier AI providers later)."""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx

from app.core.config import HTTP_MAX_CONNECTIONS, AppSettings

# httpx's own pool, made explicit: settings that start many calls at once (PUSH_MAX_PARALLEL)
# are checked against its size. Idle connections kept for reuse are httpx's default too.
LIMITS = httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=20)


@lru_cache(maxsize=1)
def tls_context() -> ssl.SSLContext:
    """Loading the CA bundle takes seconds on some machines (antivirus scanning the file), so
    it happens once per process and every client shares the context."""
    return httpx.create_ssl_context()


def build_http_client(settings: AppSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, verify=tls_context(), limits=LIMITS
    )
