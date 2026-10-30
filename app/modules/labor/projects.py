"""Projects: as many as an owner needs, open (and overlapping) until the owner closes them.
Closing keeps everything; a closed project takes no new workers, attendance or payments
until it is reopened. Renaming changes nothing else."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from fastapi import status

from app.modules.labor.audit import Audit, changes
from app.modules.labor.models import LaborProject, ProjectRepository, ProjectStatus, Rates
from app.modules.labor.rules import rules
from app.modules.labor.schemas import ProjectIn, RatesIn
from app.platform import ActorContext, AppError, ConflictError, PageParams

PROJECT_FIELDS = ("name", "kind", "description", "icon", "started_on", "notes")
RATE_FIELDS = ("by_gender", "default_minor", "female_minor", "male_minor")


class ProjectsFullError(ConflictError):
    code: ClassVar[str] = "LABOR_PROJECTS_FULL"
    default_message = "That's as many projects as one owner keeps."


class ProjectClosedError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code: ClassVar[str] = "LABOR_PROJECT_CLOSED"
    default_message = "That project is closed: reopen it first."


class ProjectStateError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code: ClassVar[str] = "LABOR_PROJECT_STATE"
    default_message = "That project is already that way."


def like(text: str) -> dict[str, Any]:
    return {"$regex": re.escape(text), "$options": "i"}


class Projects:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repository = ProjectRepository(ctx.scope, ctx.clock)
        self.audit = Audit(ctx)

    async def add(self, body: ProjectIn) -> LaborProject:
        if (
            await LaborProject.find(self.repository.query()).count()
            >= rules().projects.max_per_owner
        ):
            raise ProjectsFullError
        fields = {
            **body.model_dump(include=set(PROJECT_FIELDS)),
            "started_on": (body.started_on or self.ctx.today()).isoformat(),
            "currency": self.ctx.user.base_currency,
        }
        project = await self.repository.create(fields)
        await self.audit.record(
            "project",
            str(project.id),
            "created",
            project_id=project.id,
            changed=changes({}, fields, PROJECT_FIELDS),
        )
        return project

    async def page(
        self, params: PageParams, state: ProjectStatus | None, text: str | None
    ) -> tuple[list[LaborProject], int]:
        criteria: dict[str, Any] = {"status": state} if state else {}
        if text:
            criteria["$or"] = [{"name": like(text)}, {"kind": like(text)}]
        # Open ones first, then the latest started.
        order = [("status", -1), ("started_on", -1), ("_id", -1)]
        return await self.repository.list(params, order, criteria)

    async def get(self, project_id: str) -> LaborProject:
        return await self.repository.get(project_id)

    async def open_one(self, project_id: str) -> LaborProject:
        """The project, to change what is in it: refused while it is closed."""
        project = await self.get(project_id)
        if project.status is ProjectStatus.CLOSED:
            raise ProjectClosedError(details={"project_id": project_id})
        return project

    async def change(self, project_id: str, body: ProjectIn) -> LaborProject:
        before = await self.get(project_id)
        fields = {
            **body.model_dump(include=set(PROJECT_FIELDS)),
            "started_on": (body.started_on.isoformat() if body.started_on else before.started_on),
        }
        changed = changes(before.model_dump(), fields, PROJECT_FIELDS)
        if not changed:
            return before
        project = await self.repository.update(project_id, fields)
        await self.audit.record(
            "project", project_id, "updated", project_id=project.id, changed=changed
        )
        return project

    async def set_status(
        self, project_id: str, state: ProjectStatus, note: str | None
    ) -> LaborProject:
        before = await self.get(project_id)
        if before.status is state:
            raise ProjectStateError(details={"status": state})
        closing = state is ProjectStatus.CLOSED
        project = await self.repository.update(
            project_id, {"status": state, "closed_at": self.ctx.clock.now() if closing else None}
        )
        await self.audit.record(
            "project",
            project_id,
            "closed" if closing else "reopened",
            project_id=project.id,
            changed=changes({"status": before.status}, {"status": state}, ("status",)),
            note=note,
        )
        return project

    async def set_rates(self, project_id: str, body: RatesIn) -> LaborProject:
        """The defaults a new worker's wage starts from. Workers already registered keep
        theirs: a default is never applied backwards."""
        before = await self.open_one(project_id)
        rates = Rates(**body.model_dump(exclude={"note"}))
        changed = changes(before.rates.model_dump(), rates.model_dump(), RATE_FIELDS)
        if not changed:
            return before
        project = await self.repository.update(project_id, {"rates": rates.model_dump()})
        await self.audit.record(
            "rates",
            project_id,
            "rates_changed",
            project_id=project.id,
            changed=changed,
            note=body.note,
        )
        return project
