"""How a CSV file is read for import (data/imports.yaml)."""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.modules.expenses.domain import EntryKind

DATA = Path(__file__).parents[2] / "data" / "imports.yaml"


class ImportField(StrEnum):
    """What a column can hold. An amount comes from ``amount`` (with ``kind`` or its sign) or
    from ``debit`` and ``credit`` columns, as bank statements have them."""

    DATE = "date"
    TIME = "time"
    TITLE = "title"
    AMOUNT = "amount"
    DEBIT = "debit"
    CREDIT = "credit"
    KIND = "kind"
    CURRENCY = "currency"
    CATEGORY = "category"
    QUANTITY = "quantity"
    UNIT = "unit"
    MERCHANT = "merchant"
    PAYMENT_MODE = "payment_mode"
    FOR_WHOM = "for_whom"
    TAGS = "tags"
    NOTE = "note"


class _Limits(BaseModel):
    max_bytes: int = Field(ge=1)
    max_rows: int = Field(ge=1)
    preview_rows: int = Field(ge=1)
    max_problems: int = Field(ge=1)
    header_search_rows: int = Field(ge=1)
    commit_batch: int = Field(ge=1)


class ImportRules(BaseModel):
    limits: _Limits
    columns: dict[ImportField, list[str]]
    kinds: dict[EntryKind, list[str]]
    date_formats: list[str] = Field(min_length=1)
    time_formats: list[str]
    tag_separators: list[str]
    fallback_category: dict[EntryKind, str]


@cache
def import_rules() -> ImportRules:
    return ImportRules.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))
