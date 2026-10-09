"""The set of modules this app instance runs, with their routers mounted under ``/v1``."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from beanie import Document
from fastapi import APIRouter, Depends

from app.platform.modules.deps import module_gate
from app.platform.modules.discovery import LoadedModule
from app.platform.modules.errors import UnknownModuleError


class ModuleRegistry:
    def __init__(self, modules: Sequence[LoadedModule]) -> None:
        self._modules = {module.manifest.key: module for module in modules}

    def __iter__(self) -> Iterator[LoadedModule]:
        return iter(self._modules.values())

    def __len__(self) -> int:
        return len(self._modules)

    def get(self, key: str) -> LoadedModule:
        module = self._modules.get(key)
        if module is None:
            raise UnknownModuleError
        return module

    def documents(self) -> list[type[Document]]:
        return [document for module in self for document in module.documents]

    def mount(self, api: APIRouter) -> None:
        """Module routes live under ``/v1/<slug>`` behind the availability gate (switched off by
        an admin or by the user -> 403 MODULE_DISABLED); admin routes under ``/v1/admin/<slug>``
        stay reachable so admins can still manage a module's data while it is off."""
        for module in self:
            key, slug = module.manifest.key, module.manifest.slug
            if module.router is not None:
                api.include_router(
                    module.router,
                    prefix=f"/{slug}",
                    tags=[key],
                    dependencies=[Depends(module_gate(key))],
                )
            if module.admin_router is not None:
                api.include_router(
                    module.admin_router, prefix=f"/admin/{slug}", tags=[f"admin:{key}"]
                )
