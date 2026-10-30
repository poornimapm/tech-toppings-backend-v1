"""The owner's overview: today across their open projects (who is marked, what the day costs),
what is owed in each, and the latest changes. Four queries, whatever the number of projects."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from beanie import SortDirection
from pydantic import BaseModel

from app.modules.labor.models import (
    AttendanceRepository,
    AuditRepository,
    LaborAttendance,
    LaborPayment,
    LaborProject,
    LaborWorker,
    PaymentRepository,
    ProjectRepository,
    ProjectStatus,
    WorkerRepository,
)
from app.modules.labor.schemas import LaborAuditOut
from app.platform import ActorContext, PageParams

RECENT = 5


class OverviewProject(BaseModel):
    id: str
    name: str
    icon: str | None
    workers: int
    counts: dict[str, int]  # today's: full, half, absent, unmarked
    today_minor: int
    owed_minor: int


class OverviewOut(BaseModel):
    day: dt.date
    open_projects: int
    workers: int
    today_minor: int
    owed_minor: int
    projects: list[OverviewProject]
    recent: list[LaborAuditOut]


async def _grouped(model: type[Any], pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = await model.get_pymongo_collection().aggregate(pipeline)
    return [row async for row in cursor]


class Overview:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx

    async def today(self) -> OverviewOut:
        scope, clock, day = self.ctx.scope, self.ctx.clock, self.ctx.today()
        projects = (
            await LaborProject.find(
                ProjectRepository(scope, clock).query({"status": ProjectStatus.OPEN})
            )
            .sort([("started_on", SortDirection.DESCENDING)])
            .to_list()
        )
        ids = [project.id for project in projects]
        mine = {"project_id": {"$in": ids}}
        workers = {
            row["_id"]: row["count"]
            for row in await _grouped(
                LaborWorker,
                [
                    {"$match": WorkerRepository(scope, clock).query({**mine, "active": True})},
                    {"$group": {"_id": "$project_id", "count": {"$sum": 1}}},
                ],
            )
        }
        marks: dict[Any, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in await _grouped(
            LaborAttendance,
            [
                {
                    "$match": AttendanceRepository(scope, clock).query(
                        {**mine, "day": day.isoformat()}
                    )
                },
                {
                    "$group": {
                        "_id": {"project": "$project_id", "status": "$status"},
                        "count": {"$sum": 1},
                        "amount": {"$sum": "$amount_minor"},
                    }
                },
            ],
        ):
            counts = marks[row["_id"]["project"]]
            counts[row["_id"]["status"]] += row["count"]
            counts["amount"] += row["amount"]
        owed: dict[Any, int] = defaultdict(int)
        for model, repository, sign, extra in (
            (LaborAttendance, AttendanceRepository(scope, clock), 1, {}),
            (LaborPayment, PaymentRepository(scope, clock), -1, {"voided_at": None}),
        ):
            for row in await _grouped(
                model,
                [
                    {"$match": repository.query({**mine, **extra})},
                    {"$group": {"_id": "$project_id", "sum": {"$sum": "$amount_minor"}}},
                ],
            ):
                owed[row["_id"]] += sign * row["sum"]

        rows = []
        for project in projects:
            counts = marks[project.id]
            marked = counts["full"] + counts["half"] + counts["absent"]
            rows.append(
                OverviewProject(
                    id=str(project.id),
                    name=project.name,
                    icon=project.icon,
                    workers=workers.get(project.id, 0),
                    counts={
                        "full": counts["full"],
                        "half": counts["half"],
                        "absent": counts["absent"],
                        "unmarked": max(workers.get(project.id, 0) - marked, 0),
                    },
                    today_minor=counts["amount"],
                    owed_minor=owed[project.id],
                )
            )
        recent, _ = await AuditRepository(scope, clock).list(
            PageParams(1, RECENT), [("at", -1), ("_id", -1)]
        )
        return OverviewOut(
            day=day,
            open_projects=len(projects),
            workers=sum(row.workers for row in rows),
            today_minor=sum(row.today_minor for row in rows),
            owed_minor=sum(row.owed_minor for row in rows),
            projects=rows,
            recent=[LaborAuditOut.of(entry) for entry in recent],
        )
