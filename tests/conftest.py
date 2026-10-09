from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from app.core.config import Settings
from tests.helpers import (
    TEST_MONGO_BASE_URI,
    FrozenClock,
    build_app,
    build_settings,
    running_client,
    with_database,
)

SETTINGS_ENV_PREFIXES = (
    "APP_",
    "MONGO_",
    "LOG_",
    "CORS_",
    "SECURITY_",
    "AUTH_",
    "RATE_LIMIT_",
    "API_",
    "DEFAULT_",
    "MODULES_",
    "BOOTSTRAP_ADMIN_",
)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment() -> Iterator[None]:
    """Tests must not depend on the developer's shell or .env (poe loads .env into the env)."""
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.startswith(SETTINGS_ENV_PREFIXES):
                patch.delenv(name)
        yield


@pytest.fixture(scope="session")
def test_db_name() -> str:
    return f"tt-test-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def mongo_uri(test_db_name: str) -> str:
    return with_database(TEST_MONGO_BASE_URI, test_db_name)


@pytest.fixture(scope="session", autouse=True)
async def _drop_test_database(test_db_name: str) -> AsyncIterator[None]:
    yield
    client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(
        TEST_MONGO_BASE_URI, serverSelectionTimeoutMS=2000
    )
    try:
        await client.drop_database(test_db_name)
    except PyMongoError:
        pass  # MongoDB unavailable: the tests that needed it have already failed loudly.
    finally:
        await client.close()


@pytest.fixture
def settings(mongo_uri: str) -> Settings:
    return build_settings(mongo_uri)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def app(settings: Settings, clock: FrozenClock) -> FastAPI:
    return build_app(settings, clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with running_client(app) as http:
        yield http


@pytest.fixture
async def other_client(
    client: AsyncClient,  # noqa: ARG001 - requested so the app lifespan is already running
    app: FastAPI,
) -> AsyncIterator[AsyncClient]:
    """A second browser (own cookie jar) against the same running app."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
