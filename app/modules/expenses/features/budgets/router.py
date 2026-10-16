from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.expenses.features.budgets.schemas import BudgetIn, BudgetOut, BudgetPatch
from app.modules.expenses.features.budgets.service import Budgets
from app.platform import ActorDep, Page, PageParamsDep, error_responses

router = APIRouter(prefix="/budgets")


def budgets(ctx: ActorDep) -> Budgets:
    return Budgets(ctx)


BudgetsDep = Annotated[Budgets, Depends(budgets)]


@router.get(
    "",
    summary="My budgets, each with how it stands today (newest first)",
    responses=error_responses(401, 403),
)
async def list_budgets(service: BudgetsDep, params: PageParamsDep) -> Page[BudgetOut]:
    items, total = await service.overview(params)
    return Page[BudgetOut].build(items, params, total)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a budget for all spending, a category or a tag (a trip, an event)",
    responses=error_responses(401, 403, 409),
)
async def add_budget(body: BudgetIn, service: BudgetsDep) -> BudgetOut:
    return await service.create(body)


@router.patch(
    "/{budget_id}",
    summary="Change a budget (only what is sent)",
    responses=error_responses(401, 403, 404),
)
async def change_budget(budget_id: str, body: BudgetPatch, service: BudgetsDep) -> BudgetOut:
    return await service.update(budget_id, body)


@router.delete(
    "/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a budget (its entries stay)",
    responses=error_responses(401, 403, 404),
)
async def delete_budget(budget_id: str, service: BudgetsDep) -> None:
    await service.delete(budget_id)
