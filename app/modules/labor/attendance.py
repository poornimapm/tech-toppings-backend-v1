"""Attendance: one mark per worker, project and day (full, half or absent), the day's sheet
with its summary, and what each mark earned at the rate it was recorded with.

A worker's first mark of a day keeps their wage at that moment (``rate_minor``); changing the
mark later recomputes its amount from that same rate, so a wage change never rewrites the past.
A second mark for the same day is the same record, changed (a unique index makes sure).

A mark kept offline and sent later carries the one its sender saw (``expected``): if the day
was marked otherwise in the meantime, it is refused rather than overwriting the newer mark."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

from beanie import PydanticObjectId, SortDirection
from fastapi import status
from pymongo.errors import DuplicateKeyError

from app.modules.labor.audit import Audit, changes
from app.modules.labor.models import (
    AttendanceRepository,
    AttendanceStatus,
    LaborAttendance,
    LaborWageChange,
    LaborWorker,
    WageChangeRepository,
    WorkerRepository,
)
from app.modules.labor.projects import Projects
from app.modules.labor.rules import earned, rules
from app.modules.labor.schemas import MarkIn
from app.modules.labor.workers import wage_on
from app.platform import ActorContext, AppError

MARK_FIELDS = ("status", "amount_minor", "note")
UNMARKED = "unmarked"


class MarkChangedError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code: ClassVar[str] = "LABOR_MARK_CHANGED"
    default_message = "That mark was changed elsewhere since it was made: it is left as it is."


class DayInFutureError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "LABOR_DAY_IN_FUTURE"
    default_message = "Attendance is recorded for today or a day before it."


class UnknownWorkerError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "LABOR_WORKER_UNKNOWN"
    default_message = "That worker isn't in this project (or is inactive)."


@dataclass
class DaySheet:
    day: date
    workers: list[LaborWorker]
    marks: dict[str, LaborAttendance] = field(default_factory=dict)  # by worker id

    def counts(self) -> dict[str, int]:
        found = {status.value: 0 for status in AttendanceStatus}
        for mark in self.marks.values():
            found[mark.status.value] += 1
        found["unmarked"] = sum(1 for worker in self.workers if str(worker.id) not in self.marks)
        return found

    @property
    def total_minor(self) -> int:
        return sum(mark.amount_minor for mark in self.marks.values())


class Attendance:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.projects = Projects(ctx)
        self.repository = AttendanceRepository(ctx.scope, ctx.clock)
        self.workers = WorkerRepository(ctx.scope, ctx.clock)
        self.wages = WageChangeRepository(ctx.scope, ctx.clock)
        self.audit = Audit(ctx)

    async def sheet(self, project_id: str, day: date) -> DaySheet:
        """The day: the active workers, and any inactive one marked that day."""
        project = await self.projects.get(project_id)
        marks = await LaborAttendance.find(
            self.repository.query({"project_id": project.id, "day": day.isoformat()})
        ).to_list()
        marked = {mark.worker_id for mark in marks}
        workers = (
            await LaborWorker.find(
                self.workers.query(
                    {
                        "project_id": project.id,
                        "$or": [{"active": True}, {"_id": {"$in": list(marked)}}],
                    }
                )
            )
            .sort([("code", SortDirection.ASCENDING), ("_id", SortDirection.ASCENDING)])
            .to_list()
        )
        return DaySheet(day, workers, {str(mark.worker_id): mark for mark in marks})

    async def mark(self, project_id: str, day: date, marks: list[MarkIn]) -> DaySheet:
        """Each worker's mark for the day: new ones at the worker's wage on that day, changed
        ones at the rate they were first recorded with."""
        if day > self.ctx.today():
            raise DayInFutureError(details={"day": day.isoformat()})
        project = await self.projects.open_one(project_id)
        sheet = await self.sheet(project_id, day)
        workers = {str(worker.id): worker for worker in sheet.workers}
        for given in marks:
            worker = workers.get(given.worker_id)
            existing = sheet.marks.get(given.worker_id)
            if worker is None or (existing is None and not worker.active):
                raise UnknownWorkerError(details={"worker_id": given.worker_id})
            # A tap kept offline says what it saw: changed since to something else, it is old.
            now = existing.status.value if existing else UNMARKED
            if given.expected is not None and now not in {given.expected, given.status.value}:
                raise MarkChangedError(details={"worker_id": given.worker_id, "now": now})
        chosen = [workers[given.worker_id] for given in marks]
        history = await self._wages(project.id, [worker.id for worker in chosen])
        for given, worker in zip(marks, chosen, strict=True):
            on_day = history.get(str(worker.id), [])
            rate = wage_on(on_day, day.isoformat(), worker.daily_wage_minor)
            await self._one(project.id, day, worker, given, sheet, rate)
        return await self.sheet(project_id, day)

    async def _wages(
        self, project_id: PydanticObjectId | None, worker_ids: list[PydanticObjectId | None]
    ) -> dict[str, list[LaborWageChange]]:
        """These workers' wage changes, by worker id."""
        criteria = {"project_id": project_id, "worker_id": {"$in": worker_ids}}
        found: dict[str, list[LaborWageChange]] = {}
        for change in await LaborWageChange.find(self.wages.query(criteria)).to_list():
            found.setdefault(str(change.worker_id), []).append(change)
        return found

    async def _one(
        self,
        project_id: PydanticObjectId | None,
        day: date,
        worker: LaborWorker,
        given: MarkIn,
        sheet: DaySheet,
        rate: int,
    ) -> None:
        units = rules().wages.units[given.status.value]
        existing = sheet.marks.get(str(worker.id))
        if existing is None:
            fields: dict[str, Any] = {
                "project_id": project_id,
                "worker_id": worker.id,
                "day": day.isoformat(),
                "status": given.status,
                "units": units,
                "rate_minor": rate,
                "amount_minor": earned(rate, units),
                "note": given.note,
            }
            try:
                created = await self.repository.create(fields)
            except DuplicateKeyError:  # marked at the same moment elsewhere: change that one
                again = await self.repository.first(
                    {"project_id": project_id, "worker_id": worker.id, "day": day.isoformat()}
                )
                if again is None:
                    raise
                # ...unless this mark was kept offline and saw it unmarked: the new one stays.
                if given.expected is not None and again.status.value not in {
                    given.expected,
                    given.status.value,
                }:
                    raise MarkChangedError(
                        details={"worker_id": str(worker.id), "now": again.status.value}
                    ) from None
                sheet.marks[str(worker.id)] = again
                await self._one(project_id, day, worker, given, sheet, rate)
                return
            await self.audit.record(
                "attendance",
                str(created.id),
                "marked",
                project_id=project_id,
                changed=changes({}, {**fields, "status": given.status.value}, MARK_FIELDS),
                note=f"{worker.code} {day.isoformat()}",
            )
            return
        after = {
            "status": given.status,
            "units": units,
            "amount_minor": earned(existing.rate_minor, units),
            "note": given.note,
        }
        changed = changes(
            {**existing.model_dump(), "status": existing.status.value},
            {**after, "status": given.status.value},
            MARK_FIELDS,
        )
        if not changed:
            return
        await self.repository.update(str(existing.id), after)
        await self.audit.record(
            "attendance",
            str(existing.id),
            "changed",
            project_id=project_id,
            changed=changed,
            note=f"{worker.code} {day.isoformat()}",
        )
