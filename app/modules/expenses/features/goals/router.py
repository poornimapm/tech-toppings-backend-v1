from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.modules.expenses.features.goals.schemas import ContributionIn, GoalIn, GoalOut, GoalPatch
from app.modules.expenses.features.goals.service import Goals
from app.platform import (
    ActorDep,
    IdempotencyKey,
    Page,
    PageParamsDep,
    answer_once,
    error_responses,
)

router = APIRouter(prefix="/goals")


def goals(ctx: ActorDep) -> Goals:
    return Goals(ctx)


GoalsDep = Annotated[Goals, Depends(goals)]


@router.get(
    "",
    summary="My goals, each with how far it has come (newest first)",
    responses=error_responses(401, 403),
)
async def list_goals(service: GoalsDep, params: PageParamsDep) -> Page[GoalOut]:
    items, total = await service.overview(params)
    return Page[GoalOut].build(items, params, total)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a goal, funded by hand or by net savings (once per Idempotency-Key)",
    responses=error_responses(401, 403),
)
async def add_goal(
    body: GoalIn, service: GoalsDep, response: Response, key: IdempotencyKey = None
) -> GoalOut:
    return answer_once(response, await service.create(body, key))


@router.patch(
    "/{goal_id}",
    summary="Change a goal (only what is sent), as of the revision edited",
    responses=error_responses(401, 403, 404, 409),
)
async def change_goal(goal_id: str, body: GoalPatch, service: GoalsDep) -> GoalOut:
    return await service.update(goal_id, body)


@router.delete(
    "/{goal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a goal (it can be brought back)",
    responses=error_responses(401, 403, 404),
)
async def delete_goal(goal_id: str, service: GoalsDep) -> None:
    await service.delete(goal_id)


@router.post(
    "/{goal_id}/undelete",
    summary="Bring a deleted goal back",
    responses=error_responses(401, 403, 404),
)
async def undelete_goal(goal_id: str, service: GoalsDep) -> GoalOut:
    return await service.undelete(goal_id)


@router.post(
    "/{goal_id}/contributions",
    status_code=status.HTTP_201_CREATED,
    summary="Add money to a goal funded by hand, or take some out (once per Idempotency-Key)",
    responses=error_responses(401, 403, 404, 409),
)
async def contribute(
    goal_id: str,
    body: ContributionIn,
    service: GoalsDep,
    response: Response,
    key: IdempotencyKey = None,
) -> GoalOut:
    return answer_once(response, await service.contribute(goal_id, body, key))
