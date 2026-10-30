"""Reports: what was earned (from attendance, at its recorded rates) and what was paid (from
payments not voided), by day, week or month, by worker, and what is still owed. The two are
never mixed: earnings are worked out, payments are what changed hands.

The database adds up each day; days are grouped into weeks (Monday first) and months here,
which keeps a year's report to a few hundred rows whatever the number of workers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, ClassVar

from beanie import SortDirection
from fastapi import status

from app.modules.labor.models import (
    AttendanceRepository,
    LaborAttendance,
    LaborPayment,
    LaborProject,
    LaborWorker,
    PaymentRepository,
    WorkerRepository,
)
from app.modules.labor.projects import Projects
from app.modules.labor.rules import rules
from app.modules.labor.workers import Workers
from app.platform import ActorContext, AppError


class Grouping(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class PeriodTooLongError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "LABOR_PERIOD_INVALID"
    default_message = "A report runs from a day to a later one, within the longest period taken."


class ExportTooLargeError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "LABOR_EXPORT_TOO_LARGE"
    default_message = "That's more days and payments than one file takes. Choose a shorter period."


@dataclass
class Totals:
    full: int = 0
    half: int = 0
    absent: int = 0
    days: float = 0  # payable days: a half day is 0.5
    earned_minor: int = 0
    paid_minor: int = 0

    @property
    def owed_minor(self) -> int:
        return self.earned_minor - self.paid_minor

    def add(self, other: Totals) -> None:
        self.full += other.full
        self.half += other.half
        self.absent += other.absent
        self.days += other.days
        self.earned_minor += other.earned_minor
        self.paid_minor += other.paid_minor


@dataclass
class Report:
    start: date
    end: date
    grouping: Grouping
    periods: list[tuple[date, Totals]] = field(default_factory=list)
    workers: dict[str, Totals] = field(default_factory=dict)  # in the period, by worker id
    to_date: dict[str, Totals] = field(default_factory=dict)  # earned and paid up to the end
    names: dict[str, LaborWorker] = field(default_factory=dict)  # the project's, by id
    total: Totals = field(default_factory=Totals)
    currency: str = ""  # the project's


@dataclass
class History:
    worker: LaborWorker
    start: date
    end: date
    marks: list[LaborAttendance]
    payments: list[LaborPayment]  # voided ones too: they show


def period_start(day: date, grouping: Grouping) -> date:
    if grouping is Grouping.WEEK:
        return day - timedelta(days=day.weekday())
    if grouping is Grouping.MONTH:
        return day.replace(day=1)
    return day


def _span(start: date, end: date) -> dict[str, str]:
    """The days from ``start`` to ``end``, refused if they run backwards or for too long."""
    if end < start or (end - start).days + 1 > rules().reports.max_days:
        raise PeriodTooLongError(details={"max_days": rules().reports.max_days})
    return {"$gte": start.isoformat(), "$lte": end.isoformat()}


class Reports:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.projects = Projects(ctx)
        self.attendance = AttendanceRepository(ctx.scope, ctx.clock)
        self.payments = PaymentRepository(ctx.scope, ctx.clock)
        self.workers = WorkerRepository(ctx.scope, ctx.clock)

    async def _earned(
        self, criteria: Mapping[str, Any], by: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        pipeline: list[dict[str, Any]] = [
            {"$match": criteria},
            {
                "$group": {
                    "_id": by,
                    "full": {"$sum": {"$cond": [{"$eq": ["$status", "full"]}, 1, 0]}},
                    "half": {"$sum": {"$cond": [{"$eq": ["$status", "half"]}, 1, 0]}},
                    "absent": {"$sum": {"$cond": [{"$eq": ["$status", "absent"]}, 1, 0]}},
                    "days": {"$sum": "$units"},
                    "earned_minor": {"$sum": "$amount_minor"},
                }
            },
        ]
        cursor = await LaborAttendance.get_pymongo_collection().aggregate(pipeline)
        return [row async for row in cursor]

    async def _paid(
        self, criteria: Mapping[str, Any], by: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        pipeline = [
            {"$match": {**criteria, "voided_at": None}},
            {"$group": {"_id": by, "paid_minor": {"$sum": "$amount_minor"}}},
        ]
        cursor = await LaborPayment.get_pymongo_collection().aggregate(pipeline)
        return [row async for row in cursor]

    async def project_report(
        self, project_id: str, start: date, end: date, grouping: Grouping
    ) -> Report:
        days = _span(start, end)
        project = await self.projects.get(project_id)
        mine = {"project_id": project.id}
        by_day_worker = {"day": "$day", "worker": "$worker_id"}
        earned = await self._earned(self.attendance.query({**mine, "day": days}), by_day_worker)
        paid = await self._paid(self.payments.query({**mine, "day": days}), by_day_worker)

        periods: dict[date, Totals] = defaultdict(Totals)
        workers: dict[str, Totals] = defaultdict(Totals)
        for row in earned:
            totals = Totals(
                row["full"], row["half"], row["absent"], row["days"], row["earned_minor"], 0
            )
            periods[period_start(date.fromisoformat(row["_id"]["day"]), grouping)].add(totals)
            workers[str(row["_id"]["worker"])].add(totals)
        for row in paid:
            totals = Totals(paid_minor=row["paid_minor"])
            periods[period_start(date.fromisoformat(row["_id"]["day"]), grouping)].add(totals)
            workers[str(row["_id"]["worker"])].add(totals)

        report = Report(start, end, grouping, sorted(periods.items()), dict(workers))
        report.currency = project.currency
        for totals in workers.values():
            report.total.add(totals)
        report.to_date = await self._to_date(project, end)
        found = await LaborWorker.find(self.workers.query(mine)).to_list()
        report.names = {str(worker.id): worker for worker in found}
        return report

    async def entries(
        self, project_id: str, start: date, end: date
    ) -> tuple[list[LaborAttendance], list[LaborPayment]]:
        """Every day marked and every payment (not voided) between two days, oldest first, for
        a file: refused before anything is read when there are more than one file takes."""
        days = _span(start, end)
        project = await self.projects.get(project_id)
        mine = {"project_id": project.id, "day": days}
        marked = LaborAttendance.find(self.attendance.query(mine))
        paid = LaborPayment.find(self.payments.query({**mine, "voided_at": None}))
        most = rules().reports.export_max_rows
        if await marked.count() + await paid.count() > most:
            raise ExportTooLargeError(details={"max": most})
        oldest = [("day", SortDirection.ASCENDING), ("_id", SortDirection.ASCENDING)]
        return await marked.sort(oldest).to_list(), await paid.sort(oldest).to_list()

    async def worker_history(self, worker_id: str, start: date, end: date) -> History:
        """A worker's marks and payments between two days, newest first."""
        days = _span(start, end)
        worker = await Workers(self.ctx).get(worker_id)
        mine = {"worker_id": worker.id, "day": days}
        newest = [("day", SortDirection.DESCENDING), ("_id", SortDirection.DESCENDING)]
        marks = await LaborAttendance.find(self.attendance.query(mine)).sort(newest).to_list()
        paid = await LaborPayment.find(self.payments.query(mine)).sort(newest).to_list()
        return History(worker, start, end, marks, paid)

    async def _to_date(self, project: LaborProject, until: date) -> dict[str, Totals]:
        """Each worker's earnings and payments, every day up to ``until`` (days recorded before
        the project's start date too): what is owed is the one less the other."""
        mine = {"project_id": project.id, "day": {"$lte": until.isoformat()}}
        found: dict[str, Totals] = defaultdict(Totals)
        for row in await self._earned(self.attendance.query(mine), {"worker": "$worker_id"}):
            found[str(row["_id"]["worker"])].earned_minor += row["earned_minor"]
        for row in await self._paid(self.payments.query(mine), {"worker": "$worker_id"}):
            found[str(row["_id"]["worker"])].paid_minor += row["paid_minor"]
        return dict(found)
