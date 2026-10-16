"""The planning features' tunables (data/planning.yaml)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from app.modules.expenses.domain import CadenceUnit

DATA = Path(__file__).parent / "data" / "planning.yaml"


class _Duplicates(BaseModel):
    window_days: int
    similarity: float
    max_results: int


class _Budgets(BaseModel):
    alert_threshold: float
    max_per_user: int


class _Ledger(BaseModel):
    retries: int
    pause_ms: int
    max_lines: int


class _Ious(BaseModel):
    remind_days_before: int


class Detect(BaseModel):
    lookback_days: int
    min_occurrences: int
    amount_tolerance: float
    cadences: dict[CadenceUnit, tuple[int, int]]  # the days apart each unit allows


class _Recurring(BaseModel):
    remind_days_before: int
    max_catch_up: int
    detect: Detect


class Planning(BaseModel):
    duplicates: _Duplicates
    budgets: _Budgets
    ledger: _Ledger
    ious: _Ious
    recurring: _Recurring


@cache
def planning() -> Planning:
    return Planning.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))
