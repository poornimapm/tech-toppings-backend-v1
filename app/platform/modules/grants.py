"""Modules only some accounts may use (manifest ``audience="granted"``): an admin gives one to
an account, or takes it back. The account's own preferences never can (``PreferencesIn`` has no
such field), and a module given to nobody is not there at all for them (``ModuleService``)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from beanie import PydanticObjectId, SortDirection
from fastapi import status

from app.core.clock import Clock
from app.core.errors import AppError
from app.core.repository import Scope
from app.platform.modules.models import UserModule
from app.platform.modules.repository import UserModuleRepository
from app.platform.users.models import User

if TYPE_CHECKING:
    from app.platform.modules.registry import ModuleRegistry


class ModuleNotGrantableError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "MODULE_NOT_GRANTABLE"
    default_message = "That module is everyone's already: there is nothing to give."


def ensure_grantable(registry: ModuleRegistry, keys: Sequence[str]) -> None:
    """Each key a module only some accounts may use (an unknown one answers 404)."""
    for key in keys:
        if registry.get(key).manifest.audience != "granted":
            raise ModuleNotGrantableError(details={"module": key})


async def set_grant(
    registry: ModuleRegistry,
    clock: Clock,
    *,
    user_id: PydanticObjectId,
    key: str,
    granted: bool,
    admin_id: PydanticObjectId | None,
) -> None:
    ensure_grantable(registry, [key])
    preferences = UserModuleRepository(Scope.for_user(user_id), clock)
    await preferences.upsert(
        key,
        {
            "granted_at": clock.now() if granted else None,
            "granted_by": admin_id if granted else None,
        },
    )


async def grants_of(user_ids: Sequence[PydanticObjectId]) -> dict[PydanticObjectId, list[str]]:
    """The modules given to each of these accounts."""
    found: dict[PydanticObjectId, list[str]] = defaultdict(list)
    rows = UserModule.find(
        {"user_id": {"$in": list(user_ids)}, "granted_at": {"$ne": None}, "deleted_at": None}
    ).sort([("module_key", SortDirection.ASCENDING)])
    async for row in rows:
        found[row.user_id].append(row.module_key)
    return found


async def granted_users(key: str) -> list[User]:
    """The accounts a module is given to, by name."""
    rows = UserModule.get_pymongo_collection().find(
        {"module_key": key, "granted_at": {"$ne": None}, "deleted_at": None}, {"user_id": 1}
    )
    ids = [row["user_id"] async for row in rows]
    return (
        await User.find({"_id": {"$in": ids}, "deleted_at": None})
        .sort([("name", SortDirection.ASCENDING)])
        .to_list()
    )
