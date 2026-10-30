"""Modules as an admin sets them: each manifest's defaults, overridden in the ``modules``
collection (status, the global switch, order). A field set back to null follows the manifest
again, so a new release's manifest changes reach it."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import NotFoundError, error_responses
from app.platform.admin.deps import AdminActionDep, AdminDep
from app.platform.modules.manifest import ModuleStatus
from app.platform.modules.models import ModuleOverride
from app.platform.modules.registry import ModuleRegistry

router = APIRouter(prefix="/modules")
FIELDS = ("status", "global_enabled", "order")


class AdminModuleOut(BaseModel):
    key: str
    manifest_status: ModuleStatus
    manifest_order: int
    status: ModuleStatus = Field(description="What users see: the override, else the manifest")
    global_enabled: bool
    order: int
    overridden: list[str] = Field(description="The fields an admin set")
    audience: Literal["everyone", "granted"] = Field(
        description="granted: only the accounts an admin gives it to (and admins) see it"
    )


class AdminModulePatch(BaseModel):
    """Only what is sent changes; null sets a field back to the manifest's."""

    model_config = ConfigDict(extra="forbid")

    status: ModuleStatus | None = None
    global_enabled: bool | None = None
    order: int | None = Field(default=None, ge=0, le=1000)


def _out(registry: ModuleRegistry, key: str, override: ModuleOverride | None) -> AdminModuleOut:
    manifest = registry.get(key).manifest
    set_fields = [name for name in FIELDS if override and getattr(override, name) is not None]
    return AdminModuleOut(
        key=key,
        manifest_status=manifest.status,
        manifest_order=manifest.order,
        audience=manifest.audience,
        status=override.status if override and override.status else manifest.status,
        global_enabled=override.global_enabled
        if override and override.global_enabled is not None
        else True,
        order=override.order if override and override.order is not None else manifest.order,
        overridden=set_fields,
    )


@router.get(
    "",
    summary="Every module, as its manifest says and as overridden",
    responses=error_responses(401, 403),
)
async def list_modules(ctx: AdminDep) -> list[AdminModuleOut]:
    registry: ModuleRegistry = ctx.state.modules
    overrides = {item.key: item for item in await ModuleOverride.find_all().to_list()}
    modules = [
        _out(registry, module.manifest.key, overrides.get(module.manifest.key))
        for module in registry
    ]
    return sorted(modules, key=lambda item: (item.order, item.key))


@router.patch(
    "/{key}",
    summary="Override a module's status, switch or order; null: the manifest's (audited)",
    responses=error_responses(401, 403, 404, 422),
)
async def change_module(key: str, body: AdminModulePatch, action: AdminActionDep) -> AdminModuleOut:
    registry: ModuleRegistry = action.ctx.state.modules
    if key not in {module.manifest.key for module in registry}:
        raise NotFoundError
    before = _out(registry, key, await ModuleOverride.find_one({"key": key}))
    sent = body.model_dump(include=body.model_fields_set, mode="json")
    if sent:
        now = action.ctx.clock.now()
        await ModuleOverride.get_pymongo_collection().update_one(
            {"key": key},
            {"$set": {**sent, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    after = _out(registry, key, await ModuleOverride.find_one({"key": key}))
    changes: dict[str, Any] = {
        name: {"from": getattr(before, name), "to": getattr(after, name)}
        for name in FIELDS
        if getattr(before, name) != getattr(after, name)
    }
    if before.overridden != after.overridden:
        # Pinned to what the manifest says, or let go: nothing shows yet, but a release will.
        changes["overridden"] = {"from": before.overridden, "to": after.overridden}
    if changes:
        await action.record("module.changed", entity="module", entity_id=key, details=changes)
    return after
