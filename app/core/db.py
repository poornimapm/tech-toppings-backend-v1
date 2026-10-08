"""MongoDB access: one PyMongo async client per process, Beanie initialised lazily.

The app must start even while the database is unreachable (Atlas maintenance, cold
network, local mongod stopped): readiness reports 503 and the first successful check
initialises Beanie, so the service heals itself without a restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from beanie import Document, init_beanie
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.core.config import MongoSettings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    def __init__(self, settings: MongoSettings, document_models: Sequence[type[Document]]) -> None:
        self._client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            settings.uri.get_secret_value(),
            maxPoolSize=settings.max_pool_size,
            minPoolSize=settings.min_pool_size,
            serverSelectionTimeoutMS=settings.server_selection_timeout_ms,
            connectTimeoutMS=settings.connect_timeout_ms,
            appname=settings.app_name,
            tz_aware=True,
        )
        self._database: AsyncDatabase[dict[str, Any]] = self._client.get_default_database()
        self._document_models = tuple(document_models)
        self._odm_ready = False
        self._init_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._database.name

    @property
    def odm_ready(self) -> bool:
        return self._odm_ready

    async def check_ready(self) -> bool:
        """Ping MongoDB and make sure Beanie is initialised. Never raises."""
        try:
            await self._client.admin.command("ping")
            if not self._odm_ready:
                await self._init_odm()
        except PyMongoError as exc:
            logger.warning("database_unavailable", database=self.name, error=type(exc).__name__)
            return False
        return True

    async def _init_odm(self) -> None:
        async with self._init_lock:
            if self._odm_ready:
                return
            await init_beanie(database=self._database, document_models=list(self._document_models))
            self._odm_ready = True
            logger.info("database_ready", database=self.name, documents=len(self._document_models))

    async def close(self) -> None:
        await self._client.close()
