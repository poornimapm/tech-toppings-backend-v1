"""Agent tools for Labor. The assistant never records attendance itself: it reads a day's workers
(``labor_day``) and hands a draft of marks to the attendance page (``labor_attendance_draft``),
which the owner checks and saves (ADR-0024)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from beanie import SortDirection
from pydantic import Field

from app.modules.labor.attendance import Attendance, DayInFutureError
from app.modules.labor.models import LaborProject, ProjectRepository, ProjectStatus
from app.modules.labor.projects import Projects
from app.modules.labor.schemas import AttendanceDraftIn, DayText
from app.platform import ActorContext, tool

LISTED = 20  # open projects named when the owner must choose one


async def _open_projects(ctx: ActorContext) -> list[LaborProject]:
    query = ProjectRepository(ctx.scope, ctx.clock).query({"status": ProjectStatus.OPEN})
    newest = [("started_on", SortDirection.DESCENDING), ("_id", SortDirection.DESCENDING)]
    return await LaborProject.find(query).sort(newest).limit(LISTED).to_list()


@tool("read")
async def labor_day(
    ctx: ActorContext,
    project_id: Annotated[
        str | None, Field(description="The project; left out: the only open one")
    ] = None,
    day: Annotated[DayText | None, Field(description="YYYY-MM-DD; today if left out")] = None,
) -> dict[str, Any]:
    """A project's workers for a day (id, name, number, and their mark if they have one).
    Without a project and with more than one open, the open projects to choose from instead."""
    if project_id is None:
        open_ = await _open_projects(ctx)
        if len(open_) != 1:
            return {"choose": [{"id": str(item.id), "name": item.name} for item in open_]}
        project_id = str(open_[0].id)
    project = await Projects(ctx).get(project_id)
    when = dt.date.fromisoformat(day) if day else ctx.today()
    sheet = await Attendance(ctx).sheet(project_id, when)
    return {
        "project_id": str(project.id),
        "project": project.name,
        "day": when.isoformat(),
        "workers": [
            {
                "id": str(worker.id),
                "name": worker.name,
                "code": worker.code,
                "status": mark.status.value if (mark := sheet.marks.get(str(worker.id))) else None,
            }
            for worker in sheet.workers
        ],
    }


@tool("client")
async def labor_attendance_draft(ctx: ActorContext, draft: AttendanceDraftIn) -> None:
    """Show the owner a day's marks to check and save, read from what they said. Nothing is
    saved until they save it."""
    if draft.project_id is not None:
        await Projects(ctx).get(draft.project_id)  # theirs, or not found
    if draft.day is not None and dt.date.fromisoformat(draft.day) > ctx.today():
        raise DayInFutureError(details={"day": draft.day})
