from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.modules.expenses.features.products.models import Product
from app.modules.expenses.features.products.prices import MerchantPrices, PriceStats, Purchase
from app.modules.expenses.units import known_unit

MAX_ALIASES = 20

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ProductOut(BaseModel):
    id: str
    name: str
    aliases: list[str]
    dimension: str
    price_unit: str
    alert_above_minor: int | None
    apart: list[str]  # ids of products the user said are different things

    @classmethod
    def of(cls, product: Product) -> ProductOut:
        return cls.model_validate({**product.model_dump(), "id": str(product.id)})


class LastPrice(BaseModel):
    unit_price_minor: int
    date: dt.date
    merchant: str | None


class ProductSummary(ProductOut):
    """A product with how its price is going, for the list."""

    currency: str
    purchases: int  # with a price that compares
    last: LastPrice | None
    change_pct: float | None
    trend: list[int]  # the latest prices, oldest first
    cheapest: str | None


class ProductDetail(ProductOut):
    currency: str
    purchases: list[Purchase]  # oldest first; unit_price_minor None = not comparable
    stats: PriceStats | None
    merchants: list[MerchantPrices]
    cheapest: str | None


class TrackIn(BaseModel):
    """Follow the price of what an entry is called (and every entry with that title)."""

    model_config = ConfigDict(extra="forbid")

    title: Name
    name: Name | None = None


class ProductPatch(BaseModel):
    """Only what is sent changes; ``alert_above_minor: null`` turns the alert off."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    aliases: Annotated[list[Name], Field(min_length=1, max_length=MAX_ALIASES)] | None = None
    price_unit: Annotated[str, AfterValidator(known_unit)] | None = Field(
        default=None, description="A code from GET /units, of the product's dimension"
    )
    alert_above_minor: int | None = Field(
        default=None, gt=0, le=10**13, description="Per price_unit, in minor units"
    )


class OtherProductIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
