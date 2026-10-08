"""Shared test helpers: hermetic settings and a running ASGI client.

Tests use a real MongoDB (default ``mongodb://localhost:27017``; override with
``TEST_MONGO_URI``) and a throwaway database per test session, dropped at the end.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import (
    AppSettings,
    CorsSettings,
    Environment,
    LogFormat,
    LogSettings,
    MongoSettings,
    SecuritySettings,
    Settings,
)

TEST_MONGO_BASE_URI = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
UNREACHABLE_MONGO_URI = "mongodb://127.0.0.1:1/tt-unreachable"


def with_database(base_uri: str, name: str) -> str:
    return urlunsplit(urlsplit(base_uri)._replace(path=f"/{name}"))


def mongo_settings(uri: str, *, server_selection_timeout_ms: int = 2000) -> MongoSettings:
    return MongoSettings(
        _env_file=None,
        uri=SecretStr(uri),
        server_selection_timeout_ms=server_selection_timeout_ms,
    )


def build_settings(mongo_uri: str, **groups: Any) -> Settings:
    """Settings that ignore any developer ``.env`` file; pass whole groups to override."""
    defaults: dict[str, Any] = {
        "app": AppSettings(_env_file=None, env=Environment.TEST),
        "mongo": mongo_settings(mongo_uri),
        "log": LogSettings(_env_file=None, format=LogFormat.JSON, level="DEBUG"),
        "cors": CorsSettings(_env_file=None),
        "security": SecuritySettings(_env_file=None),
    }
    return Settings(**(defaults | groups))


@asynccontextmanager
async def running_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Run the app's lifespan and yield an HTTP client bound to it."""
    async with LifespanManager(app) as manager:
        # raise_app_exceptions=False: assert on the 500 envelope instead of the raw exception.
        transport = ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def json_log_lines(output: str) -> list[dict[str, Any]]:
    """Parse captured stdout into JSON log events (non-JSON lines are ignored)."""
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]
