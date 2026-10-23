from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, model_validator

from app.core.deps import SettingsDep
from app.core.errors import error_responses
from app.core.pagination import Page, page_params
from app.platform.admin.deps import AdminDep
from app.platform.audit.models import AuditEntry
from app.platform.users.models import User

router = APIRouter()


class AuditOut(BaseModel):
    id: str
    at: dt.datetime
    action: str
    actor_id: str | None
    actor_email: str | None  # as the account is now (None: the system, or since deleted)
    entity: str
    entity_id: str | None
    owner_id: str | None
    owner_email: str | None
    details: dict[str, Any]
    ip: str | None
    request_id: str | None


async def _emails(ids: set[PydanticObjectId]) -> dict[PydanticObjectId, str]:
    found = await User.find({"_id": {"$in": list(ids)}}).to_list() if ids else []
    return {user.id: user.email for user in found if user.id is not None}


class AuditFilters(BaseModel):
    action: str | None = Field(default=None, max_length=64)
    entity: str | None = Field(default=None, max_length=32)
    actor_id: PydanticObjectId | None = None
    owner_id: PydanticObjectId | None = None
    start: dt.date | None = Field(default=None, alias="from", description="First day (UTC)")
    end: dt.date | None = Field(
        default=None, alias="to", lt=dt.date.max, description="Last day (UTC)"
    )

    @model_validator(mode="after")
    def in_order(self) -> AuditFilters:
        if self.start and self.end and self.start > self.end:
            raise ValueError("'from' is after 'to'")
        return self

    def criteria(self) -> dict[str, Any]:
        found: dict[str, Any] = self.model_dump(
            exclude_none=True, include={"action", "entity", "actor_id", "owner_id"}
        )
        days = {
            op: dt.datetime.combine(day, dt.time.min, dt.UTC) + dt.timedelta(days=shift)
            for op, day, shift in (("$gte", self.start, 0), ("$lt", self.end, 1))
            if day
        }
        if days:
            found["at"] = days
        return found


class AuditQuery(AuditFilters):
    """The whole query string: FastAPI reads a model from the query only when it is the
    route's one query parameter, so paging lives here too."""

    page: int = Field(default=1, ge=1)
    page_size: int | None = Field(default=None, ge=1)


@router.get(
    "/audit-logs",
    summary="The audit trail, newest first: who changed what, and on whose account",
    responses=error_responses(401, 403, 422),
)
async def list_audit_logs(
    _: AdminDep, settings: SettingsDep, query: Annotated[AuditQuery, Query()]
) -> Page[AuditOut]:
    params = page_params(settings, query.page, query.page_size)
    criteria = query.criteria()
    total = await AuditEntry.find(criteria).count()
    entries = (
        await AuditEntry.find(criteria)
        .sort("-at", "-_id")
        .skip(params.skip)
        .limit(params.page_size)
        .to_list()
    )
    people = {item for entry in entries for item in (entry.actor_id, entry.owner_id) if item}
    emails = await _emails(people)
    items = [
        AuditOut(
            id=str(entry.id),
            at=entry.at,
            action=entry.action,
            actor_id=str(entry.actor_id) if entry.actor_id else None,
            actor_email=emails.get(entry.actor_id) if entry.actor_id else None,
            entity=entry.entity,
            entity_id=entry.entity_id,
            owner_id=str(entry.owner_id) if entry.owner_id else None,
            owner_email=emails.get(entry.owner_id) if entry.owner_id else None,
            details=entry.details,
            ip=entry.ip,
            request_id=entry.request_id,
        )
        for entry in entries
    ]
    return Page[AuditOut].build(items, params, total)
