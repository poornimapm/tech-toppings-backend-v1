from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.platform.modules.models import ModuleOverride

ModuleFiles = dict[str, str]


@pytest.fixture
def module_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[dict[str, ModuleFiles]], str]:
    """Write a throwaway modules package to disk: ``{"folder": {"file.py": source}}``.

    Returns its importable name; each call gets a unique name so imports never collide."""
    monkeypatch.syspath_prepend(str(tmp_path))

    def create(modules: dict[str, ModuleFiles]) -> str:
        name = f"tt_modules_{uuid.uuid4().hex[:8]}"
        root = tmp_path / name
        root.mkdir()
        (root / "__init__.py").write_text("", encoding="utf-8")
        for folder, files in modules.items():
            (root / folder).mkdir()
            (root / folder / "__init__.py").write_text("", encoding="utf-8")
            for filename, source in files.items():
                (root / folder / filename).write_text(
                    source.replace("PKG", f"{name}.{folder}"), encoding="utf-8"
                )
        return name

    return create


@pytest.fixture
async def clean_overrides(
    client: AsyncClient,  # noqa: ARG001 - torn down before the app stops (database still open)
) -> AsyncIterator[None]:
    """Admin overrides are global, so tests that write them remove them afterwards."""
    yield
    await ModuleOverride.get_pymongo_collection().delete_many({})
