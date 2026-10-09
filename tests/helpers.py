"""Shared test helpers: hermetic settings, a controllable clock, a running ASGI client, and
small auth shortcuts.

Tests use a real MongoDB (default ``mongodb://localhost:27017``; override with
``TEST_MONGO_URI``) and a throwaway database per test session, dropped at the end.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr

from app.core.clock import Clock
from app.core.config import (
    ApiSettings,
    AppSettings,
    AuthSettings,
    BootstrapSettings,
    CorsSettings,
    DefaultsSettings,
    Environment,
    LogFormat,
    LogSettings,
    ModulesSettings,
    MongoSettings,
    RateLimitSettings,
    RegistrationMode,
    SecuritySettings,
    Settings,
)
from app.main import create_app

TEST_MONGO_BASE_URI = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
UNREACHABLE_MONGO_URI = "mongodb://127.0.0.1:1/tt-unreachable"

TEST_JWT_SECRET = "test-secret-" + "x" * 40
CSRF = {"X-TT-CSRF": "1"}
# The real modules plus the test-only ones (tests/fixtures/modules), e.g. "notes".
TEST_MODULE_PACKAGES = ("app.modules", "tests.fixtures.modules")
PASSWORD = "Correct-Horse-42"
REFRESH_COOKIE = "tt_refresh"


def with_database(base_uri: str, name: str) -> str:
    return urlunsplit(urlsplit(base_uri)._replace(path=f"/{name}"))


def mongo_settings(uri: str, *, server_selection_timeout_ms: int = 2000) -> MongoSettings:
    return MongoSettings(
        _env_file=None,
        uri=SecretStr(uri),
        server_selection_timeout_ms=server_selection_timeout_ms,
    )


def auth_settings(**overrides: Any) -> AuthSettings:
    """Fast hashing, http-friendly cookie, open registration unless a test says otherwise."""
    values: dict[str, Any] = {
        "jwt_secret": SecretStr(TEST_JWT_SECRET),
        "refresh_cookie_secure": False,  # the test client talks plain http
        "refresh_cookie_path": "/v1/auth",  # no /api proxy in front of the test app
        "registration_mode": RegistrationMode.OPEN,
        "argon2_time_cost": 1,
        "argon2_memory_kib": 8192,
    }
    return AuthSettings(_env_file=None, **(values | overrides))


def build_settings(mongo_uri: str, **groups: Any) -> Settings:
    """Settings that ignore any developer ``.env`` file; pass whole groups to override."""
    defaults: dict[str, Any] = {
        "app": AppSettings(_env_file=None, env=Environment.TEST),
        "mongo": mongo_settings(mongo_uri),
        "log": LogSettings(_env_file=None, format=LogFormat.JSON, level="DEBUG"),
        "cors": CorsSettings(_env_file=None),
        "security": SecuritySettings(_env_file=None),
        "auth": auth_settings(),
        "rate_limit": RateLimitSettings(_env_file=None, enabled=False),
        "api": ApiSettings(_env_file=None),
        "defaults": DefaultsSettings(_env_file=None),
        "modules": ModulesSettings(_env_file=None, packages=TEST_MODULE_PACKAGES),
        "bootstrap": BootstrapSettings(_env_file=None),
    }
    return Settings(**(defaults | groups))


class FrozenClock:
    """Clock that only moves when told to (starts at the real current time)."""

    def __init__(self, start: datetime | None = None) -> None:
        current = start or datetime.now(UTC)
        self._now = current.replace(microsecond=current.microsecond // 1000 * 1000)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)


def build_app(settings: Settings, clock: Clock | None = None) -> FastAPI:
    """The real app; test settings add the test-only modules (tests/fixtures/modules)."""
    return create_app(settings, clock=clock or FrozenClock())


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


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Account:
    email: str
    password: str
    access_token: str
    user_id: str

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.access_token)


def account_from(response: Response, email: str, password: str) -> Account:
    assert response.status_code in {200, 201}, response.text
    body = response.json()
    return Account(email, password, body["access_token"], body["user"]["id"])


async def register(
    client: AsyncClient, email: str | None = None, password: str = PASSWORD, name: str = "Asha"
) -> Account:
    address = email or unique_email()
    response = await client.post(
        "/v1/auth/register", json={"email": address, "name": name, "password": password}
    )
    return account_from(response, address, password)


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    return await client.post("/v1/auth/login", json={"email": email, "password": password})


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def api_operations(app: FastAPI, prefix: str = "/v1/") -> list[tuple[str, str]]:
    """(METHOD, path) of every operation the app exposes, read from its OpenAPI schema.

    FastAPI keeps included routers nested, so ``app.routes`` does not list them; the schema is
    the stable inventory (and exactly what clients see).
    """
    paths: dict[str, dict[str, object]] = app.openapi()["paths"]
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith(prefix)
        for method in operations
        if method in HTTP_METHODS
    )
