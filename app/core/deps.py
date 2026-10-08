"""FastAPI dependencies for objects owned by the app instance (settings, database).

They read from ``app.state`` rather than module globals, so every app built by
``create_app(settings)`` (e.g. one per test) is fully isolated.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.db import Database


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
