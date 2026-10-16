"""``/v1/fx``: exchange rates, for the currencies an entry can be in."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.errors import error_responses
from app.platform.context import ActorDep
from app.platform.fx.service import Fx


class RatesOut(BaseModel):
    base: str
    date: dt.date  # the day the rates are from
    rates: dict[str, str]  # currency -> how much of it one base buys (decimal strings)


router = APIRouter(prefix="/fx", tags=["fx"])


@router.get(
    "/rates",
    summary="What one unit of a currency buys of the others (default: my base currency)",
    responses=error_responses(401, 422, 503),
)
async def read_rates(
    ctx: ActorDep,
    base: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    day: Annotated[dt.date | None, Query(alias="date")] = None,
) -> RatesOut:
    found = await Fx(ctx).rates(base or ctx.user.base_currency, day)
    return RatesOut(base=found.base, date=dt.date.fromisoformat(found.rate_date), rates=found.rates)
