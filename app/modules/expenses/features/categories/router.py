from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.expenses.features.categories.service import (
    Categories,
    CategoryIn,
    CategoryOut,
    CategoryPatch,
)
from app.platform import ClockDep, Page, PageParams, UserScope, error_responses

router = APIRouter(prefix="/categories")


def categories(scope: UserScope, clock: ClockDep) -> Categories:
    return Categories(scope, clock)


CategoriesDep = Annotated[Categories, Depends(categories)]


@router.get("", summary="System categories and my own", responses=error_responses(401, 403))
async def list_categories(service: CategoriesDep) -> Page[CategoryOut]:
    items = await service.all()
    return Page[CategoryOut].build(items, PageParams(1, max(len(items), 1)), len(items))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a category of my own",
    responses=error_responses(401, 403, 409),
)
async def add_category(body: CategoryIn, service: CategoriesDep) -> CategoryOut:
    return CategoryOut.of(await service.create(body))


@router.patch(
    "/{category_id}",
    summary="Rename or restyle one of my categories",
    responses=error_responses(401, 403, 404, 409),
)
async def change_category(
    category_id: str, body: CategoryPatch, service: CategoriesDep
) -> CategoryOut:
    return CategoryOut.of(await service.update(category_id, body))


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive one of my categories (its entries keep it)",
    responses=error_responses(401, 403, 404),
)
async def archive_category(category_id: str, service: CategoriesDep) -> None:
    await service.archive(category_id)
