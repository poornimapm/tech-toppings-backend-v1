"""One running app per test session (per pytest-xdist worker), shared by every test that keeps
the default settings.

Building an app costs about a second, far more than most tests: FastAPI prepares each of the
~200 routes again for every app. With the default settings the app keeps nothing in memory a
test could leave behind (rate limiting and AI providers off, no Google sign-in), so tests can
share it. Each test still gets its own clock, its own HTTP client (cookie jar) and the database
as the app set it up. Tests with other settings build an app of their own, as before.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from beanie import Document
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.types import ASGIApp

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.db import Database
from app.core.logging import configure_logging
from tests.helpers import asgi_client, build_app, lifespan_of


class LentClock:
    """The shared app's clock: it reads the clock of the test running now."""

    def __init__(self) -> None:
        self.current: Clock = SystemClock()

    def now(self) -> datetime:
        return self.current.now()


class SharedApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.clock = LentClock()
        self.app = build_app(settings, self.clock)
        self._served: ASGIApp = self.app
        self._bound: dict[type[Document], object] = {}

    @asynccontextmanager
    async def running(self) -> AsyncIterator[SharedApp]:
        """Run the app's lifespan for the whole session."""
        async with lifespan_of(self.app) as manager:
            self._served = manager.app
            self._bound = self._bindings()
            yield self

    async def lend(self, clock: Clock) -> FastAPI:
        """The app, readied for the next test: its clock, logging, and the documents bound to
        this app's database again if another app initialised Beanie since."""
        self.clock.current = clock
        configure_logging(self.settings.log)  # as create_app does: a test may have changed it
        if any(model.get_pymongo_collection() is not bound for model, bound in self._bound.items()):
            database: Database = self.app.state.database
            database._odm_ready = False  # the app's own start-up path binds them again
            assert await database.check_ready()
            self._bound = self._bindings()
        return self.app

    def client(self) -> AsyncClient:
        """A new browser (own cookie jar) against the running app."""
        return asgi_client(self._served)

    def _bindings(self) -> dict[type[Document], object]:
        # Compared by identity: two clients of the same server compare equal.
        return {model: model.get_pymongo_collection() for model in self.app.state.documents}
