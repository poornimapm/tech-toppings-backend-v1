"""Labor's tunables (data/labor.yaml), and the one rule for rounding a day's pay."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DATA = Path(__file__).parent / "data" / "labor.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectRules(_Strict):
    kinds: tuple[str, ...]
    max_per_owner: int = Field(ge=1)


class WorkerRules(_Strict):
    max_per_project: int = Field(ge=1)
    code_prefix: str = Field(max_length=4)
    code_digits: int = Field(ge=1, le=6)
    genders: tuple[str, ...]


class FileRules(_Strict):
    types: tuple[str, ...]
    photo_max_bytes: int = Field(ge=10_000)
    id_max_bytes: int = Field(ge=10_000)
    photo_side_px: int = Field(ge=64)
    id_side_px: int = Field(ge=64)


class WageRules(_Strict):
    max_daily: int = Field(ge=1)
    units: dict[str, float]


class PaymentRules(_Strict):
    methods: tuple[str, ...]
    max_amount: int = Field(ge=1)


class ReportRules(_Strict):
    max_days: int = Field(ge=1)
    export_max_rows: int = Field(ge=1)


class ReadingRules(_Strict):
    max_chars: int = Field(ge=1)


class Rules(_Strict):
    projects: ProjectRules
    workers: WorkerRules
    files: FileRules
    wages: WageRules
    payments: PaymentRules
    reports: ReportRules
    reading: ReadingRules


@cache
def rules() -> Rules:
    return Rules.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))


def earned(rate_minor: int, units: float) -> int:
    """A day's pay: the rate times the day's units, rounded half up to whole minor units (a
    half day of ₹301.01 is ₹150.51). Decimal, so no binary fraction ever tips it."""
    pay = Decimal(rate_minor) * Decimal(str(units))
    return int(pay.to_integral_value(rounding=ROUND_HALF_UP))
