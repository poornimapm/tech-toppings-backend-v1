from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from app.platform.modules.models import ModuleOverride


@pytest.fixture
async def clean_overrides(
    client: AsyncClient,  # noqa: ARG001 - torn down before the app stops (database still open)
) -> AsyncIterator[None]:
    """Admin overrides are global, so tests that write them remove them afterwards."""
    yield
    await ModuleOverride.get_pymongo_collection().delete_many({})
