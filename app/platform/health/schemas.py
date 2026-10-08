from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthStatus(BaseModel):
    status: Literal["ok"]
    name: str
    version: str


class ReadinessStatus(BaseModel):
    status: Literal["ready"]
    checks: dict[str, Literal["ok"]]
