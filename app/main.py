"""Application factory.

Run locally with ``poe dev`` (uvicorn ``--factory app.main:create_app``). Tests call
``create_app(settings, clock=...)`` to get a fully isolated app per test.

Feature modules are discovered from ``MODULES_PACKAGES`` (ADR-0001): dropping a folder with a
``manifest.py`` into ``app/modules`` registers its documents, mounts its routes under
``/v1/<slug>`` and lists it on the Welcome page, with no edit here. Agents, skills and tools
are discovered the same way from platform sub-packages and modules (ADR-0013).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import COMMON_ERROR_RESPONSES, register_exception_handlers
from app.core.http import build_http_client
from app.core.logging import configure_logging, get_logger
from app.core.middleware import install_middleware
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordHasher
from app.platform import PLATFORM_DOCUMENTS
from app.platform.agents.catalog import discover_agents
from app.platform.agents.providers import ProviderChain
from app.platform.agents.router import router as agents_router
from app.platform.agents.runtime import AgentRuntime
from app.platform.auth.google import GoogleIdTokenVerifier
from app.platform.auth.router import router as auth_router
from app.platform.auth.router import sessions_router
from app.platform.health.router import router as health_router
from app.platform.modules.discovery import discover_modules
from app.platform.modules.registry import ModuleRegistry
from app.platform.modules.router import router as modules_router
from app.platform.plugins import subpackages
from app.platform.users.router import router as me_router

logger = get_logger(__name__)

API_V1_PREFIX = "/v1"


def create_app(settings: Settings | None = None, *, clock: Clock | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log)
    modules = ModuleRegistry(discover_modules(settings.modules.packages))
    database = Database(settings.mongo, document_models=[*PLATFORM_DOCUMENTS, *modules.documents()])
    http_client = build_http_client(settings.app)
    clock = clock or SystemClock()
    agent_packages = [
        *(p for root in settings.agent.packages for p in subpackages(root)),
        *(module.package for module in modules),
    ]
    agents = AgentRuntime(
        discover_agents(agent_packages),
        ProviderChain(settings.agent, http_client, clock),
        settings.agent,
        clock,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not await database.check_ready():
            # Keep serving: /readyz reports 503 until MongoDB answers, then heals itself.
            logger.warning("database_unavailable_at_startup", database=database.name)
        logger.info(
            "app_started",
            env=settings.app.env,
            version=settings.app.version,
            modules=[module.manifest.key for module in modules],
        )
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
    app.state.modules = modules
    app.state.agents = agents
    app.state.clock = clock
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
    api_v1.include_router(modules_router)
    api_v1.include_router(agents_router)
    modules.mount(api_v1)
    app.include_router(api_v1)
    return app
