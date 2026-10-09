"""Outbound HTTP (Google keys today; free-tier AI providers later)."""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx

from app.core.config import AppSettings


@lru_cache(maxsize=1)
def tls_context() -> ssl.SSLContext:
    """Loading the CA bundle takes seconds on some machines (antivirus scanning the file), so
    it happens once per process and every client shares the context."""
    return httpx.create_ssl_context()


def build_http_client(settings: AppSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.http_timeout_seconds, verify=tls_context())
