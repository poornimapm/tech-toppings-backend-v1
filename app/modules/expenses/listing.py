"""The list tools' tunables (data/listing.yaml)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DATA = Path(__file__).parent / "data" / "listing.yaml"


class _SavedFilters(BaseModel):
    max_per_user: int = Field(ge=1)


class _Bulk(BaseModel):
    max_entries: int = Field(ge=1)


class _Export(BaseModel):
    max_rows: int = Field(ge=1)


class Listing(BaseModel):
    saved_filters: _SavedFilters
    bulk: _Bulk
    export: _Export


@cache
def listing() -> Listing:
    return Listing.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))
