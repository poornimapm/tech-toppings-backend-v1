"""Everything under ``/v1/admin/labor``: the platform admin's view across every owner (read
only: owners keep their own records). Every route depends on the admin role; ID pictures are
never shown here, only whether a worker has one."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from app.modules.labor.audit_log import AuditFiltersDep, audit_page
from app.modules.labor.models import (
    MODULE,
    LaborProject,
    LaborWorker,
    ProjectRepository,
    ProjectStatus,
    WorkerRepository,
)
from app.modules.labor.projects import Projects, like
from app.modules.labor.report_file import filename, report_file
from app.modules.labor.reports import Grouping, Reports
from app.modules.labor.schemas import LaborAuditOut, LaborProjectOut, LaborReportOut, LaborWorkerOut
from app.modules.labor.views import report_out
from app.modules.labor.workers import Workers
from app.platform import (
    CSV_MEDIA_TYPE,
    CSV_RESPONSE,
    AdminDep,
    Page,
    PageParamsDep,
    User,
    attachment,
    error_responses,
    granted_users,
    parse_object_id,
    rate_limit,
)

router = APIRouter()


class LaborOwnerOut(BaseModel):
    id: str
    name: str
    email: str
    active: bool
    open_projects: int
    projects: int
    workers: int


class LaborAdminProjectOut(LaborProjectOut):
    owner_id: str
    owner_name: str


async def _counts(
    model: type[LaborProject] | type[LaborWorker], match: dict[str, object]
) -> dict[object, int]:
    cursor = await model.get_pymongo_collection().aggregate(
        [{"$match": match}, {"$group": {"_id": "$user_id", "count": {"$sum": 1}}}]
    )
    return {row["_id"]: row["count"] async for row in cursor}


@router.get(
    "/owners",
    summary="The accounts given Labor, with their projects and workers",
    responses=error_responses(401, 403),
)
async def list_owners(ctx: AdminDep) -> list[LaborOwnerOut]:
    owners = await granted_users(MODULE)
    projects = ProjectRepository(ctx.scope, ctx.clock)
    workers = WorkerRepository(ctx.scope, ctx.clock)
    every = await _counts(LaborProject, projects.query())
    open_ = await _counts(LaborProject, projects.query({"status": ProjectStatus.OPEN}))
    active = await _counts(LaborWorker, workers.query({"active": True}))
    return [
        LaborOwnerOut(
            id=str(owner.id),
            name=owner.name,
            email=owner.email,
            active=owner.is_active,
            open_projects=open_.get(owner.id, 0),
            projects=every.get(owner.id, 0),
            workers=active.get(owner.id, 0),
        )
        for owner in owners
    ]


@router.get(
    "/projects",
    summary="Every owner's projects, open ones first",
    responses=error_responses(401, 403, 422),
)
async def list_projects(
    ctx: AdminDep,
    params: PageParamsDep,
    owner: str | None = None,
    state: ProjectStatus | None = None,
    q: Annotated[str | None, Query(max_length=80)] = None,
) -> Page[LaborAdminProjectOut]:
    criteria: dict[str, object] = {}
    if owner:
        criteria["user_id"] = parse_object_id(owner)
    if state:
        criteria["status"] = state
    if q:
        criteria["name"] = like(q)
    found, total = await ProjectRepository(ctx.scope, ctx.clock).list(
        params, [("status", -1), ("started_on", -1), ("_id", -1)], criteria
    )
    owners = {project.user_id for project in found}
    names = {
        str(user.id): user.name
        for user in await User.find({"_id": {"$in": list(owners)}}).to_list()
    }
    items = [
        LaborAdminProjectOut(
            **LaborProjectOut.of(project).model_dump(),
            owner_id=str(project.user_id),
            owner_name=names.get(str(project.user_id), ""),
        )
        for project in found
    ]
    return Page[LaborAdminProjectOut].build(items, params, total)


@router.get("/projects/{project_id}/workers", responses=error_responses(401, 403, 404, 422))
async def list_workers(
    project_id: str, ctx: AdminDep, params: PageParamsDep
) -> Page[LaborWorkerOut]:
    found, total = await Workers(ctx).page(project_id, params, None, None)
    return Page[LaborWorkerOut].build([LaborWorkerOut.of(item) for item in found], params, total)


@router.get("/projects/{project_id}/report", responses=error_responses(401, 403, 404, 422))
async def project_report(
    project_id: str,
    ctx: AdminDep,
    start: Annotated[dt.date, Query(alias="from")],
    end: Annotated[dt.date, Query(alias="to")],
    group: Grouping = Grouping.DAY,
) -> LaborReportOut:
    await Projects(ctx).get(project_id)
    return report_out(await Reports(ctx).project_report(project_id, start, end, group))


@router.get(
    "/projects/{project_id}/report.csv",
    summary="A project's report as a CSV file, as its owner gets it",
    response_class=Response,
    responses={**CSV_RESPONSE, **error_responses(401, 403, 404, 422, 429)},
    dependencies=[Depends(rate_limit("export"))],
)
async def project_report_file(
    project_id: str,
    ctx: AdminDep,
    start: Annotated[dt.date, Query(alias="from")],
    end: Annotated[dt.date, Query(alias="to")],
    group: Grouping = Grouping.DAY,
) -> Response:
    text = await report_file(ctx, project_id, start, end, group)
    return Response(text, media_type=CSV_MEDIA_TYPE, headers=attachment(filename(start, end)))


@router.get(
    "/audit",
    summary="Every owner's changes, newest first",
    responses=error_responses(401, 403, 422),
)
async def labor_audit(
    ctx: AdminDep, params: PageParamsDep, filters: AuditFiltersDep
) -> Page[LaborAuditOut]:
    return await audit_page(ctx, params, filters)
