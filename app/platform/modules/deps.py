from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import Depends, Request

from app.core.deps import ClockDep, SettingsDep
from app.platform.auth.deps import UserScope
from app.platform.modules.service import ModuleService

if TYPE_CHECKING:
    from app.platform.modules.registry import ModuleRegistry


def get_registry(request: Request) -> ModuleRegistry:
    return cast("ModuleRegistry", request.app.state.modules)


def get_module_service(
    request: Request, scope: UserScope, clock: ClockDep, settings: SettingsDep
) -> ModuleService:
    return ModuleService(get_registry(request), scope, clock, settings.modules)


ModuleServiceDep = Annotated[ModuleService, Depends(get_module_service)]


def module_gate(key: str) -> Callable[[ModuleService], Awaitable[None]]:
    """Router dependency for a module's routes: signed in, and the module is available to the
    caller. Runs before the endpoint, so a switched-off module never touches its data."""

    async def dependency(service: ModuleServiceDep) -> None:
        await service.ensure_available(key)

    return dependency
