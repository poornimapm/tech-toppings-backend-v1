from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.modules.expenses.features.ious.schemas import (
    IouIn,
    IouOut,
    IouPatch,
    IouState,
    IouSummary,
    SettlementIn,
)
from app.modules.expenses.features.ious.service import Ious
from app.platform import (
    ActorDep,
    IdempotencyKey,
    Page,
    PageParamsDep,
    answer_once,
    error_responses,
)

router = APIRouter(prefix="/ious")


def ious(ctx: ActorDep) -> Ious:
    return Ious(ctx)


IousDep = Annotated[Ious, Depends(ious)]


@router.get(
    "",
    summary="My loans, most recently lent first: open, settled or all, with one person or all",
    responses=error_responses(401, 403),
)
async def list_ious(
    service: IousDep,
    params: PageParamsDep,
    state: IouState = IouState.OPEN,
    person: Annotated[
        str | None, Query(max_length=60, description="A person's name (as compared: any case)")
    ] = None,
) -> Page[IouOut]:
    items, total = await service.overview(params, state, person)
    return Page[IouOut].build(items, params, total)


@router.get(
    "/summary",
    summary="What is owed to me and what I owe, in all and by person",
    responses=error_responses(401, 403),
)
async def summarise_ious(service: IousDep) -> IouSummary:
    return await service.summary()


@router.get(
    "/{iou_id}",
    summary="One loan, with what was paid back on it",
    responses=error_responses(401, 403, 404),
)
async def get_iou(iou_id: str, service: IousDep) -> IouOut:
    return await service.one(iou_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Note money lent or borrowed (once per Idempotency-Key)",
    responses=error_responses(401, 403),
)
async def add_iou(
    body: IouIn, service: IousDep, response: Response, key: IdempotencyKey = None
) -> IouOut:
    return answer_once(response, await service.create(body, key))


@router.patch(
    "/{iou_id}",
    summary="Change a loan (only what is sent), as of the revision edited",
    responses=error_responses(401, 403, 404, 409),
)
async def change_iou(iou_id: str, body: IouPatch, service: IousDep) -> IouOut:
    return await service.update(iou_id, body)


@router.delete(
    "/{iou_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a loan (it can be brought back)",
    responses=error_responses(401, 403, 404),
)
async def delete_iou(iou_id: str, service: IousDep) -> None:
    await service.delete(iou_id)


@router.post(
    "/{iou_id}/undelete",
    summary="Bring a deleted loan back",
    responses=error_responses(401, 403, 404),
)
async def undelete_iou(iou_id: str, service: IousDep) -> IouOut:
    return await service.undelete(iou_id)


@router.post(
    "/{iou_id}/settlements",
    status_code=status.HTTP_201_CREATED,
    summary="Record money paid back on a loan, or correct a payment (once per Idempotency-Key)",
    responses=error_responses(401, 403, 404, 409),
)
async def settle_iou(
    iou_id: str,
    body: SettlementIn,
    service: IousDep,
    response: Response,
    key: IdempotencyKey = None,
) -> IouOut:
    return answer_once(response, await service.settle(iou_id, body, key))
