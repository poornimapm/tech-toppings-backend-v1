"""``/v1/modules``: the module catalog as the signed-in user sees it, plus their preferences.

Module keys are a shared catalog, not owned records: every call reads or changes only the
caller's own view of a module (their ``user_modules`` record)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, status

from app.core.errors import error_responses
from app.core.pagination import Page, PageParamsDep, parse_sort
from app.platform.modules.deps import ModuleServiceDep
from app.platform.modules.manifest import ModuleStatus
from app.platform.modules.schemas import ModuleView, OrderIn, PreferencesIn

router = APIRouter(prefix="/modules", tags=["modules"])

SORT_FIELDS = ("position", "key")


@router.get("", summary="Modules for the Welcome page", responses=error_responses(401))
async def list_modules(
    service: ModuleServiceDep,
    params: PageParamsDep,
    status_filter: Annotated[ModuleStatus | None, Query(alias="status")] = None,
    pinned: Annotated[bool | None, Query(description="Only (un)pinned modules")] = None,
    sort: Annotated[
        str | None, Query(description="position (default) or key; - = descending")
    ] = None,
) -> Page[ModuleView]:
    arranged = await service.arranged()
    matching = [
        item
        for item in arranged
        if (status_filter is None or item.status is status_filter)
        and (pinned is None or item.pinned is pinned)
    ]
    positions = {item.key: index for index, item in enumerate(arranged)}
    for field, direction in reversed(parse_sort(sort, SORT_FIELDS, "position")):
        if field == "key":
            matching.sort(key=lambda item: item.key, reverse=direction < 0)
        else:
            matching.sort(key=lambda item: positions[item.key], reverse=direction < 0)
    page = matching[params.skip : params.skip + params.page_size]
    return Page[ModuleView].build(await service.views(page, arranged), params, len(matching))


@router.put(
    "/order",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Arrange my modules",
    responses=error_responses(401, 404),
)
async def reorder_modules(body: OrderIn, service: ModuleServiceDep) -> None:
    await service.reorder(body.keys)


@router.get("/{key}", summary="One module", responses=error_responses(401, 404))
async def read_module(key: str, service: ModuleServiceDep) -> ModuleView:
    return await service.view(key)


@router.patch(
    "/{key}/preferences",
    summary="Pin, unpin, turn on or off for me",
    responses=error_responses(401, 404),
)
async def update_preferences(
    key: str, body: PreferencesIn, service: ModuleServiceDep
) -> ModuleView:
    return await service.update_preferences(key, body)


@router.post(
    "/{key}/interest",
    summary="Notify me when this module launches",
    responses=error_responses(401, 404, 409),
)
async def request_launch_notice(key: str, service: ModuleServiceDep) -> ModuleView:
    return await service.set_interest(key, wanted=True)


@router.delete(
    "/{key}/interest", summary="Stop the launch notice", responses=error_responses(401, 404)
)
async def cancel_launch_notice(key: str, service: ModuleServiceDep) -> ModuleView:
    return await service.set_interest(key, wanted=False)


@router.get(
    "/{key}/settings/schema",
    summary="JSON Schema of the module's settings",
    responses=error_responses(401, 404),
)
async def read_settings_schema(key: str, service: ModuleServiceDep) -> dict[str, Any]:
    return service.settings_schema(key)


@router.get(
    "/{key}/settings", summary="My settings for a module", responses=error_responses(401, 404)
)
async def read_settings(key: str, service: ModuleServiceDep) -> dict[str, Any]:
    return await service.read_settings(key)


@router.put(
    "/{key}/settings",
    summary="Save my settings for a module",
    responses=error_responses(401, 404, 422),
)
async def save_settings(
    key: str, service: ModuleServiceDep, values: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    return await service.save_settings(key, values)
