"""Workers: registered into one project (never shared with another), each with a number of
their own, a daily wage, and a photo and a picture of their ID if given. A worker is made
inactive, never removed; a wage change is kept with the day it took effect, and past
attendance keeps the rate it was recorded with. Every change needs the worker's project open."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from fastapi import Request, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.modules.labor.audit import Audit, changes
from app.modules.labor.models import (
    MODULE,
    Gender,
    LaborProject,
    LaborWageChange,
    LaborWorker,
    ProjectRepository,
    WageChangeRepository,
    WorkerRepository,
)
from app.modules.labor.projects import Projects, like
from app.modules.labor.rules import rules
from app.modules.labor.schemas import WageIn, WorkerIn
from app.platform import (
    ActorContext,
    AppError,
    ConflictError,
    Files,
    NotFoundError,
    PageParams,
    StoredFile,
    parse_object_id,
)

WORKER_FIELDS = ("name", "code", "gender", "phone", "notes")
# The worker's two pictures, by the field that points at each.
PICTURES = {"photo": "photo_id", "id_card": "id_card_id"}
MAX_WAGE_CHANGES = 500  # a worker's wage history, as it is read back


class WorkersFullError(ConflictError):
    code: ClassVar[str] = "LABOR_WORKERS_FULL"
    default_message = "That's as many workers as one project keeps."


class WorkerCodeTakenError(ConflictError):
    code: ClassVar[str] = "LABOR_WORKER_CODE_TAKEN"
    default_message = "Another worker in this project has that number."


class WorkerStateError(ConflictError):
    code: ClassVar[str] = "LABOR_WORKER_STATE"
    default_message = "That worker is already that way."


class WageDateError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "LABOR_WAGE_DATE_INVALID"
    default_message = "A new wage starts today or on a day before it."


def wage_on(history: Sequence[LaborWageChange], day: str, current: int) -> int:
    """The daily wage on ``day`` (ISO): of the changes in effect by then, the one made last (a
    later change outranks an older one from the same day or before). Before every change, the
    wage the earliest one changed from; with no history, ``current``."""
    in_effect = [change for change in history if change.effective_on <= day]
    if in_effect:
        return max(in_effect, key=lambda change: (change.created_at, str(change.id))).to_minor
    if not history:
        return current
    first = min(history, key=lambda change: (change.effective_on, change.created_at))
    return first.to_minor if first.from_minor is None else first.from_minor


def default_wage(project: LaborProject, gender: Gender | None) -> int:
    """A new worker's wage from the project's defaults: by gender where the project uses that."""
    rates = project.rates
    if rates.by_gender and gender is Gender.FEMALE:
        return rates.female_minor
    if rates.by_gender and gender is Gender.MALE:
        return rates.male_minor
    return rates.default_minor


class Workers:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.projects = Projects(ctx)
        self.repository = WorkerRepository(ctx.scope, ctx.clock)
        self.wages = WageChangeRepository(ctx.scope, ctx.clock)
        self.files = Files(ctx.scope, ctx.clock)
        self.audit = Audit(ctx)

    async def _next_code(self, project: LaborProject) -> str:
        """The project's next worker number ("W001"), taken at once so two never share one."""
        found = await LaborProject.get_pymongo_collection().find_one_and_update(
            ProjectRepository(self.ctx.scope, self.ctx.clock).query({"_id": project.id}),
            {"$inc": {"next_code": 1}},
            return_document=ReturnDocument.BEFORE,
        )
        number = int(found["next_code"]) if found else project.next_code
        settings = rules().workers
        return f"{settings.code_prefix}{number:0{settings.code_digits}d}"

    async def add(self, project_id: str, body: WorkerIn) -> LaborWorker:
        project = await self.projects.open_one(project_id)
        mine = self.repository.query({"project_id": project.id})
        if await LaborWorker.find(mine).count() >= rules().workers.max_per_project:
            raise WorkersFullError
        wage = body.daily_wage_minor
        if wage is None:
            wage = default_wage(project, body.gender)
        fields: dict[str, Any] = {
            **body.model_dump(include=set(WORKER_FIELDS)),
            "project_id": project.id,
            "code": body.code or await self._next_code(project),
            "daily_wage_minor": wage,
        }
        try:
            worker = await self.repository.create(fields)
        except DuplicateKeyError as error:
            raise WorkerCodeTakenError(details={"code": fields["code"]}) from error
        await self.wages.create(
            {
                "project_id": project.id,
                "worker_id": worker.id,
                "from_minor": None,
                "to_minor": wage,
                "effective_on": self.ctx.today().isoformat(),
            }
        )
        await self.audit.record(
            "worker",
            str(worker.id),
            "created",
            project_id=project.id,
            changed=changes({}, fields, (*WORKER_FIELDS, "daily_wage_minor")),
        )
        return worker

    async def page(
        self, project_id: str, params: PageParams, active: bool | None, text: str | None
    ) -> tuple[list[LaborWorker], int]:
        project = await self.projects.get(project_id)
        criteria: dict[str, Any] = {"project_id": project.id}
        if active is not None:
            criteria["active"] = active
        if text:
            criteria["$or"] = [{"name": like(text)}, {"code": like(text)}]
        order = [("active", -1), ("code", 1), ("_id", 1)]
        return await self.repository.list(params, order, criteria)

    async def get(self, worker_id: str) -> LaborWorker:
        worker = await self.repository.first({"_id": parse_object_id(worker_id)})
        if worker is None:
            raise NotFoundError
        return worker

    async def _editable(self, worker_id: str) -> LaborWorker:
        """The worker, to change: refused while their project is closed."""
        worker = await self.get(worker_id)
        await self.projects.open_one(str(worker.project_id))
        return worker

    async def change(self, worker_id: str, body: WorkerIn) -> LaborWorker:
        before = await self._editable(worker_id)
        fields = {**body.model_dump(include=set(WORKER_FIELDS)), "code": body.code or before.code}
        changed = changes(before.model_dump(), fields, WORKER_FIELDS)
        if not changed:
            return before
        try:
            worker = await self.repository.update(worker_id, fields)
        except DuplicateKeyError as error:
            raise WorkerCodeTakenError(details={"code": fields["code"]}) from error
        await self.audit.record(
            "worker", worker_id, "updated", project_id=before.project_id, changed=changed
        )
        return worker

    async def set_wage(self, worker_id: str, body: WageIn) -> LaborWorker:
        """A new daily wage from a day on. Attendance already recorded keeps its rate."""
        before = await self._editable(worker_id)
        effective = body.effective_on or self.ctx.today()
        if effective > self.ctx.today():
            raise WageDateError(details={"effective_on": effective.isoformat()})
        history = await self._wages_of(before)
        start = effective.isoformat()
        # Nothing changes if every day from ``start`` on is at this wage already.
        turns = {start, *(change.effective_on for change in history if change.effective_on > start)}
        new = body.daily_wage_minor
        if all(wage_on(history, day, before.daily_wage_minor) == new for day in turns):
            return before
        was = wage_on(history, start, before.daily_wage_minor)
        worker = await self.repository.update(
            worker_id, {"daily_wage_minor": body.daily_wage_minor}
        )
        await self.wages.create(
            {
                "project_id": before.project_id,
                "worker_id": before.id,
                "from_minor": was,
                "to_minor": new,
                "effective_on": start,
                "reason": body.reason,
            }
        )
        await self.audit.record(
            "worker",
            worker_id,
            "wage_changed",
            project_id=before.project_id,
            changed=changes(
                {"daily_wage_minor": was},
                {"daily_wage_minor": new, "effective_on": start},
                ("daily_wage_minor", "effective_on"),
            ),
            note=body.reason,
        )
        return worker

    async def wage_history(self, worker_id: str) -> list[LaborWageChange]:
        return await self._wages_of(await self.get(worker_id))

    async def _wages_of(self, worker: LaborWorker) -> list[LaborWageChange]:
        """The worker's wage changes, the latest made first."""
        found, _ = await self.wages.list(
            PageParams(1, MAX_WAGE_CHANGES),
            [("created_at", -1), ("_id", -1)],
            {"worker_id": worker.id},
        )
        return found

    async def set_active(self, worker_id: str, *, active: bool, note: str | None) -> LaborWorker:
        before = await self._editable(worker_id)
        if before.active is active:
            raise WorkerStateError(details={"active": active})
        worker = await self.repository.update(worker_id, {"active": active})
        await self.audit.record(
            "worker",
            worker_id,
            "activated" if active else "deactivated",
            project_id=before.project_id,
            changed=changes({"active": before.active}, {"active": active}, ("active",)),
            note=note,
        )
        return worker

    # ------------------------------------------------------------------ pictures

    async def put_picture(self, worker_id: str, which: str, request: Request) -> LaborWorker:
        """A photo or ID picture, replacing the one before (which is removed for good)."""
        before = await self._editable(worker_id)
        limits = rules().files
        stored = await self.files.keep(
            request,
            module=MODULE,
            purpose=f"worker_{which}",
            types=limits.types,
            max_bytes=limits.photo_max_bytes if which == "photo" else limits.id_max_bytes,
        )
        field = PICTURES[which]
        worker = await self.repository.update(worker_id, {field: str(stored.id)})
        await self.files.forget(getattr(before, field))
        # That it changed is said, never the picture: it is the worker's own.
        await self.audit.record(
            "worker", worker_id, f"{which}_changed", project_id=before.project_id
        )
        return worker

    async def picture(self, worker_id: str, which: str) -> StoredFile:
        worker = await self.get(worker_id)
        return await self.files.get(getattr(worker, PICTURES[which]) or "", module=MODULE)

    async def remove_picture(self, worker_id: str, which: str) -> LaborWorker:
        before = await self._editable(worker_id)
        field = PICTURES[which]
        if getattr(before, field) is None:
            return before
        worker = await self.repository.update(worker_id, {field: None})
        await self.files.forget(getattr(before, field))
        await self.audit.record(
            "worker", worker_id, f"{which}_removed", project_id=before.project_id
        )
        return worker
