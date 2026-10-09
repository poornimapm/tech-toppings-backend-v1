"""Application factory.

Run locally with ``poe dev`` (uvicorn ``--factory app.main:create_app``). Tests call
``create_app(settings, clock=...)`` to get a fully isolated app per test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from beanie import Document
from fastapi import APIRouter, FastAPI

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import COMMON_ERROR_RESPONSES, register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import install_middleware
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordHasher
from app.platform import PLATFORM_DOCUMENTS
from app.platform.auth.google import GoogleIdTokenVerifier
from app.platform.auth.router import router as auth_router
from app.platform.auth.router import sessions_router
from app.platform.health.router import router as health_router
from app.platform.users.router import router as me_router

logger = get_logger(__name__)

API_V1_PREFIX = "/v1"


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    documents: Sequence[type[Document]] = (),
) -> FastAPI:
    """Build the app. ``documents`` registers extra Beanie models (module discovery, tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log)
    database = Database(settings.mongo, document_models=[*PLATFORM_DOCUMENTS, *documents])
    http_client = httpx.AsyncClient(timeout=settings.app.http_timeout_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not await database.check_ready():
            # Keep serving: /readyz reports 503 until MongoDB answers, then heals itself.
            logger.warning("database_unavailable_at_startup", database=database.name)
        logger.info("app_started", env=settings.app.env, version=settings.app.version)
        try:
            yield
        finally:
            await http_client.aclose()
            await database.close()
            logger.info("app_stopped")

    docs = settings.app.docs_enabled
    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
        responses=COMMON_ERROR_RESPONSES,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.clock = clock or SystemClock()
    app.state.rate_limiter = RateLimiter(settings.rate_limit)
    app.state.password_hasher = PasswordHasher(settings.auth)
    app.state.google_verifier = (
        GoogleIdTokenVerifier(settings.auth, settings.auth.google_client_id, http_client)
        if settings.auth.google_client_id
        else None
    )

    register_exception_handlers(app)
    install_middleware(app, settings)
    app.include_router(health_router)

    api_v1 = APIRouter(prefix=API_V1_PREFIX)
    api_v1.include_router(auth_router)
    api_v1.include_router(me_router)
    api_v1.include_router(sessions_router)
    app.include_router(api_v1)
    return app
