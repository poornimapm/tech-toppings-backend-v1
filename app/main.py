"""Application factory.

Run locally with ``poe dev`` (uvicorn ``--factory app.main:create_app``). Tests call
``create_app(settings)`` to get a fully isolated app per test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import COMMON_ERROR_RESPONSES, register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import install_middleware
from app.platform import PLATFORM_DOCUMENTS
from app.platform.health.router import router as health_router

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log)
    database = Database(settings.mongo, document_models=PLATFORM_DOCUMENTS)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not await database.check_ready():
            # Keep serving: /readyz reports 503 until MongoDB answers, then heals itself.
            logger.warning("database_unavailable_at_startup", database=database.name)
        logger.info("app_started", env=settings.app.env, version=settings.app.version)
        try:
            yield
        finally:
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

    register_exception_handlers(app)
    install_middleware(app, settings)
    app.include_router(health_router)
    return app
