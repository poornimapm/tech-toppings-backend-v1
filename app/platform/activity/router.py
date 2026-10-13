"""``/v1/activity``: my recent activity, newest first."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.errors import error_responses
from app.core.pagination import Page, PageParamsDep
from app.core.repository import NEWEST_FIRST
from app.platform.activity.log import Activity, ActivityLog
from app.platform.context import ActorDep

router = APIRouter(prefix="/activity", tags=["dashboard"])


class ActivityOut(BaseModel):
    id: str
    type: str
    params: dict[str, Any]
    at: datetime

    @classmethod
    def of(cls, entry: Activity) -> ActivityOut:
        return cls(id=str(entry.id), type=entry.type, params=entry.params, at=entry.created_at)


@router.get("", summary="My recent activity", responses=error_responses(401))
async def list_activity(ctx: ActorDep, params: PageParamsDep) -> Page[ActivityOut]:
    log = ActivityLog(ctx.scope, ctx.clock, ctx.settings)
    entries, total = await log.list(params, NEWEST_FIRST)
    return Page[ActivityOut].build([ActivityOut.of(entry) for entry in entries], params, total)
