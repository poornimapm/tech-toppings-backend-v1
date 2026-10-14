"""What a product cost each time it was bought, and the figures drawn from that."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Any

from pydantic import BaseModel

from app.modules.expenses.units import unit_price

COUNT = "count"
PERCENT = 100


class Purchase(BaseModel):
    entry_id: str
    date: dt.date
    merchant: str | None
    quantity: float | None
    unit: str | None
    amount_minor: int
    unit_price_minor: int | None  # per the product's price unit; None = not comparable


class PriceStats(BaseModel):
    count: int
    min_minor: int
    max_minor: int
    avg_minor: int
    last_minor: int
    previous_minor: int | None
    change_pct: float | None  # last against the purchase before it


class MerchantPrices(BaseModel):
    merchant: str | None  # None: bought somewhere not noted
    count: int
    min_minor: int
    avg_minor: int
    last_minor: int


def purchase(row: Mapping[str, Any], dimension: str, per: str) -> Purchase:
    """One entry as a purchase of the product. Without a quantity, something counted is taken
    as one of it ("coffee 25" is 25 a cup); anything else can't be compared per unit."""
    quantity, unit = row.get("quantity"), row.get("unit")
    if quantity is None and dimension == COUNT:
        price: int | None = unit_price(row["amount_minor"], 1, per, per)
    else:
        price = unit_price(row["amount_minor"], quantity, unit, per) if quantity and unit else None
    return Purchase(
        entry_id=str(row["_id"]),
        date=row["date"],
        merchant=row.get("merchant"),
        quantity=quantity,
        unit=unit,
        amount_minor=row["amount_minor"],
        unit_price_minor=price,
    )


def _prices(purchases: Sequence[Purchase]) -> list[int]:
    return [item.unit_price_minor for item in purchases if item.unit_price_minor is not None]


def stats(purchases: Sequence[Purchase]) -> PriceStats | None:
    """Over purchases oldest first; ``None`` when none of them has a comparable price."""
    prices = _prices(purchases)
    if not prices:
        return None
    previous = prices[-2] if len(prices) > 1 else None
    return PriceStats(
        count=len(prices),
        min_minor=min(prices),
        max_minor=max(prices),
        avg_minor=round(fmean(prices)),
        last_minor=prices[-1],
        previous_minor=previous,
        change_pct=round((prices[-1] - previous) / previous * PERCENT, 1) if previous else None,
    )


def by_merchant(purchases: Sequence[Purchase]) -> list[MerchantPrices]:
    """Each place it was bought, cheapest on average first."""
    places: dict[str | None, list[Purchase]] = {}
    for item in purchases:
        places.setdefault(item.merchant, []).append(item)
    found = []
    for merchant, bought in places.items():
        if figures := stats(bought):
            found.append(
                MerchantPrices(
                    merchant=merchant,
                    count=figures.count,
                    min_minor=figures.min_minor,
                    avg_minor=figures.avg_minor,
                    last_minor=figures.last_minor,
                )
            )
    return sorted(found, key=lambda place: (place.avg_minor, -place.count))


def cheapest(places: Sequence[MerchantPrices]) -> str | None:
    """Where it costs least on average, among the places that were noted."""
    return next((place.merchant for place in places if place.merchant), None)
