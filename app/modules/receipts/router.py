"""The module's routes, under ``/v1/<its slug>``: every one is the signed-in user's own."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.receipts.schemas import ReceiptsItemIn, ReceiptsItemOut, ReceiptsOptions
from app.modules.receipts.service import ReceiptsItems
from app.modules.receipts.warranty import WarrantyStatus, rules
from app.platform import ActorDep, Page, PageParamsDep, error_responses

router = APIRouter()


def receipts(ctx: ActorDep) -> ReceiptsItems:
    return ReceiptsItems(ctx)


ReceiptsDep = Annotated[ReceiptsItems, Depends(receipts)]


@router.get(
    "/options",
    summary="What the receipt form offers and starts with",
    responses=error_responses(401, 403),
)
async def receipt_options() -> ReceiptsOptions:
    warranty = rules().warranty
    return ReceiptsOptions(
        remind_days_before=warranty.remind_days_before,
        max_remind_days=warranty.max_remind_days,
        max_months=warranty.max_months,
        quick_months=list(warranty.quick_months),
    )


@router.post(
    "/items",
    status_code=status.HTTP_201_CREATED,
    summary="Keep a receipt, reminded before its warranty ends",
    responses=error_responses(401, 403, 422),
)
async def add_item(body: ReceiptsItemIn, service: ReceiptsDep) -> ReceiptsItemOut:
    return ReceiptsItemOut.of(await service.add(body), service.today)


@router.get(
    "/items",
    summary="My receipts, latest bought first: all, by where their warranty stands, or by words",
    responses=error_responses(401, 403, 422),
)
async def list_items(
    service: ReceiptsDep,
    params: PageParamsDep,
    sort: Annotated[str | None, Query()] = None,
    warranty: WarrantyStatus | None = None,
    q: Annotated[
        str | None, Query(max_length=80, description="Words in its name, shop, numbers or note")
    ] = None,
) -> Page[ReceiptsItemOut]:
    items, total = await service.page(params, sort, warranty, q)
    out = [ReceiptsItemOut.of(item, service.today) for item in items]
    return Page[ReceiptsItemOut].build(out, params, total)


@router.get("/items/{item_id}", responses=error_responses(401, 403, 404))
async def get_item(item_id: str, service: ReceiptsDep) -> ReceiptsItemOut:
    return ReceiptsItemOut.of(await service.get(item_id), service.today)


@router.patch("/items/{item_id}", responses=error_responses(401, 403, 404, 422))
async def change_item(item_id: str, body: ReceiptsItemIn, service: ReceiptsDep) -> ReceiptsItemOut:
    return ReceiptsItemOut.of(await service.change(item_id, body), service.today)


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 403, 404),
)
async def delete_item(item_id: str, service: ReceiptsDep) -> None:
    await service.delete(item_id)
