from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.expenses.features.products.schemas import (
    OtherProductIn,
    ProductDetail,
    ProductOut,
    ProductPatch,
    ProductSummary,
    TrackIn,
)
from app.modules.expenses.features.products.service import Products
from app.platform import ActorDep, Page, PageParamsDep, error_responses

router = APIRouter(prefix="/products")


def products(ctx: ActorDep) -> Products:
    return Products(ctx)


ProductsDep = Annotated[Products, Depends(products)]


@router.get(
    "",
    summary="What I buy, with how its price is going (recently bought first)",
    responses=error_responses(401, 403),
)
async def list_products(
    service: ProductsDep,
    params: PageParamsDep,
    title: Annotated[
        str | None, Query(max_length=120, description="Only the one it claims")
    ] = None,
) -> Page[ProductSummary]:
    items, total = await service.overview(params, title)
    return Page[ProductSummary].build(items, params, total)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Follow the price of what an entry is called",
    responses=error_responses(401, 403, 409),
)
async def track_product(body: TrackIn, service: ProductsDep) -> ProductOut:
    return ProductOut.of(await service.track(body))


@router.get(
    "/{product_id}",
    summary="A product: every purchase, its price per unit, and where it's cheapest",
    responses=error_responses(401, 403, 404),
)
async def read_product(
    product_id: str,
    service: ProductsDep,
    start: Annotated[dt.date | None, Query(alias="from", description="First day")] = None,
    end: Annotated[dt.date | None, Query(alias="to", description="Last day, inclusive")] = None,
) -> ProductDetail:
    return await service.detail(product_id, start, end)


@router.patch(
    "/{product_id}",
    summary="Rename a product, change its names, price unit or price alert",
    responses=error_responses(401, 403, 404, 409),
)
async def change_product(product_id: str, body: ProductPatch, service: ProductsDep) -> ProductOut:
    return ProductOut.of(await service.update(product_id, body))


@router.post(
    "/{product_id}/merge",
    summary="Combine another product into this one (its names and entries join it)",
    responses=error_responses(401, 403, 404),
)
async def merge_product(product_id: str, body: OtherProductIn, service: ProductsDep) -> ProductOut:
    return ProductOut.of(await service.merge(product_id, body.product_id))


@router.post(
    "/{product_id}/apart",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Say two products are different things (no more suggestions to combine them)",
    responses=error_responses(401, 403, 404),
)
async def keep_apart(product_id: str, body: OtherProductIn, service: ProductsDep) -> None:
    await service.keep_apart(product_id, body.product_id)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop following a product (its entries stay)",
    responses=error_responses(401, 403, 404),
)
async def stop_product(product_id: str, service: ProductsDep) -> None:
    await service.stop(product_id)
