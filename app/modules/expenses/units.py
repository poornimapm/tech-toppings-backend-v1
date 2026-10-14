"""Units of quantity (data/units.yaml) and what a purchase costs per unit."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

DATA = Path(__file__).parent / "data" / "units.yaml"


@dataclass(frozen=True)
class Unit:
    code: str
    dimension: str
    factor: Decimal  # in the dimension's first unit


class UnitOut(BaseModel):
    code: str
    dimension: str
    factor: float


@cache
def units() -> dict[str, Unit]:
    data: dict[str, dict[str, float]] = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    return {
        code: Unit(code, dimension, Decimal(str(factor)))
        for dimension, table in data.items()
        for code, factor in table.items()
    }


def known_unit(code: str) -> str:
    if code not in units():
        raise ValueError(f"unknown unit; one of {', '.join(units())}")
    return code


def price_unit_for(code: str) -> str:
    """The unit a price is best quoted per: the unit itself ("per dozen", "per packet"), unless
    it is a fraction of a bigger one (grams are priced per kg, ml per litre)."""
    unit = units()[code]
    if unit.factor >= 1:
        return code
    return next(other.code for other in units().values() if other.dimension == unit.dimension)


def unit_price(amount_minor: int, quantity: float, unit: str, per: str) -> int | None:
    """What ``quantity`` ``unit`` for ``amount_minor`` costs per one ``per``, in minor units;
    ``None`` when the two units measure different things."""
    bought, priced = units()[unit], units()[per]
    if bought.dimension != priced.dimension:
        return None
    per_base = Decimal(amount_minor) / (Decimal(str(quantity)) * bought.factor)
    return int((per_base * priced.factor).to_integral_value(ROUND_HALF_UP))
