from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.expenses.features.saved_filters.schemas import (
    SavedFilterIn,
    SavedFilterOut,
    SavedFilterPatch,
)
from app.modules.expenses.features.saved_filters.service import SavedFilters
from app.platform import ActorDep, error_responses

router = APIRouter(prefix="/saved-filters")


def saved_filters(ctx: ActorDep) -> SavedFilters:
    return SavedFilters(ctx)


SavedFiltersDep = Annotated[SavedFilters, Depends(saved_filters)]


@router.get(
    "", summary="My saved views of the entries list, by name", responses=error_responses(401, 403)
)
async def list_saved_filters(service: SavedFiltersDep) -> list[SavedFilterOut]:
    return await service.all()


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Save the entries list's filters under a name",
    responses=error_responses(401, 403, 409),
)
async def add_saved_filter(body: SavedFilterIn, service: SavedFiltersDep) -> SavedFilterOut:
    return await service.create(body)


@router.patch(
    "/{saved_id}",
    summary="Rename a view, or keep the list's filters as they are now",
    responses=error_responses(401, 403, 404, 409),
)
async def change_saved_filter(
    saved_id: str, body: SavedFilterPatch, service: SavedFiltersDep
) -> SavedFilterOut:
    return await service.update(saved_id, body)


@router.delete(
    "/{saved_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a view (the entries stay)",
    responses=error_responses(401, 403, 404),
)
async def delete_saved_filter(saved_id: str, service: SavedFiltersDep) -> None:
    await service.delete(saved_id)
