"""Reading the audit trail: newest first, narrowed by project, record, action, who did it,
words and days. An owner reads their own; an admin reads anyone's (``owner``)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.modules.labor.models import (
    AuditRepository,
    LaborAuditEntry,
    LaborWorker,
    WorkerRepository,
)
from app.modules.labor.projects import like
from app.modules.labor.schemas import LaborAuditOut
from app.platform import ActorContext, Page, PageParams, parse_object_id

Key = Annotated[str | None, Field(default=None, max_length=40, pattern=r"^[a-z_]+$")]
WORKER_FIELDS = frozenset({"worker_id"})  # change fields that hold a worker's id


class AuditFilters(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str | None = None
    entity: Key = None  # project, worker, rates, attendance, payment
    entity_id: str | None = None
    action: Key = None
    actor: str | None = Field(default=None, max_length=80, description="Words in who did it")
    start: dt.date | None = Field(default=None, alias="from")
    end: dt.date | None = Field(default=None, alias="to")
    owner: str | None = Field(default=None, description="An owner's id (admins only)")

    def criteria(self, timezone: str) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if self.project_id:
            found["project_id"] = parse_object_id(self.project_id)
        for name in ("entity", "entity_id", "action"):
            if value := getattr(self, name):
                found[name] = value
        if self.actor:
            found["actor_name"] = like(self.actor)
        moments: dict[str, dt.datetime] = {}
        if self.start:
            moments["$gte"] = _midnight(self.start, timezone)
        if self.end:
            moments["$lt"] = _midnight(self.end + dt.timedelta(days=1), timezone)
        if moments:
            found["at"] = moments
        return found


def _midnight(day: dt.date, timezone: str) -> dt.datetime:
    """The day's start in the reader's time zone, as a UTC instant (entries are stored in UTC)."""
    return dt.datetime.combine(day, dt.time(), ZoneInfo(timezone)).astimezone(dt.UTC)


def audit_filters(  # noqa: PLR0913, PLR0917 - each filter is a query parameter of its own
    project_id: str | None = None,
    entity: Annotated[
        str | None, Query(max_length=40, pattern=r"^[a-z_]+$", description="project, worker ...")
    ] = None,
    entity_id: str | None = None,
    action: Annotated[str | None, Query(max_length=40, pattern=r"^[a-z_]+$")] = None,
    actor: Annotated[str | None, Query(max_length=80, description="Words in who did it")] = None,
    start: Annotated[dt.date | None, Query(alias="from")] = None,
    end: Annotated[dt.date | None, Query(alias="to")] = None,
    owner: Annotated[str | None, Query(description="An owner's id (admins only)")] = None,
) -> AuditFilters:
    return AuditFilters(
        project_id=project_id,
        entity=entity,
        entity_id=entity_id,
        action=action,
        actor=actor,
        start=start,
        end=end,
        owner=owner,
    )


AuditFiltersDep = Annotated[AuditFilters, Depends(audit_filters)]


async def _worker_names(ctx: ActorContext, entries: list[LaborAuditEntry]) -> dict[str, str]:
    """The workers these entries' changes name by id, as "name (number)": a payment says whom
    it paid. Read in the reader's own scope, like the entries."""
    ids = {
        PydanticObjectId(value)
        for entry in entries
        for change in entry.changes
        if change.field in WORKER_FIELDS
        for value in (change.before, change.after)
        if isinstance(value, str) and PydanticObjectId.is_valid(value)
    }
    if not ids:
        return {}
    workers = WorkerRepository(ctx.scope, ctx.clock).query({"_id": {"$in": list(ids)}})
    return {
        str(worker.id): f"{worker.name} ({worker.code})"
        for worker in await LaborWorker.find(workers).to_list()
    }


async def audit_page(
    ctx: ActorContext, params: PageParams, filters: AuditFilters
) -> Page[LaborAuditOut]:
    repository = AuditRepository(ctx.scope, ctx.clock)
    criteria = filters.criteria(ctx.user.timezone)
    if filters.owner and ctx.scope.owner_id is None:  # an admin's unrestricted scope
        criteria["user_id"] = parse_object_id(filters.owner)
    found, total = await repository.list(params, [("at", -1), ("_id", -1)], criteria)
    names = await _worker_names(ctx, found)
    items = [LaborAuditOut.of(entry, names) for entry in found]
    return Page[LaborAuditOut].build(items, params, total)
