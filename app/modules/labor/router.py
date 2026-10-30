"""The owner's routes, under ``/v1/labor``: every record is the signed-in owner's own (another
owner's project, worker or payment answers 404), and the module is there only for accounts an
admin gave it to (and admins). Each route names one record; ids are the owner's alone."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.modules.labor.attendance import Attendance
from app.modules.labor.audit_log import AuditFiltersDep, audit_page
from app.modules.labor.models import ProjectStatus
from app.modules.labor.overview import Overview, OverviewOut
from app.modules.labor.payments import Payments
from app.modules.labor.projects import Projects
from app.modules.labor.report_file import filename, report_file
from app.modules.labor.reports import Grouping, Reports
from app.modules.labor.rules import rules
from app.modules.labor.schemas import (
    ActiveIn,
    LaborAuditOut,
    LaborDayOut,
    LaborOptions,
    LaborPaymentOut,
    LaborProjectOut,
    LaborReportOut,
    LaborWageOut,
    LaborWorkerHistoryOut,
    LaborWorkerOut,
    MarksIn,
    MarksReadOut,
    PaymentIn,
    ProjectIn,
    RatesIn,
    ReadMarksIn,
    StatusIn,
    VoidIn,
    WageIn,
    WorkerIn,
)
from app.modules.labor.views import day_out, history_out, marks_read_out, report_out
from app.modules.labor.workers import Workers
from app.platform import (
    CSV_MEDIA_TYPE,
    CSV_RESPONSE,
    ActorDep,
    IdempotencyKey,
    Page,
    PageParamsDep,
    answer_once,
    attachment,
    error_responses,
    file_response,
    rate_limit,
    run_skill,
    to_minor,
)

router = APIRouter()
MARKS_SKILL = "mark-attendance"
MARKS_TOOL = "labor_attendance_draft"
Words = Annotated[str | None, Query(max_length=80, description="Words in a name or number")]
Day = Annotated[dt.date, Query(description="YYYY-MM-DD")]


def projects(ctx: ActorDep) -> Projects:
    return Projects(ctx)


def workers(ctx: ActorDep) -> Workers:
    return Workers(ctx)


ProjectsDep = Annotated[Projects, Depends(projects)]
WorkersDep = Annotated[Workers, Depends(workers)]
PICTURE = error_responses(401, 403, 404, 409, 413, 415)
# The picture is the request body itself (no form encoding, so nothing spills to disk).
IMAGE_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            kind: {"schema": {"type": "string", "format": "binary"}} for kind in rules().files.types
        },
    }
}


@router.get(
    "/options", summary="What the forms offer and check", responses=error_responses(401, 403)
)
async def labor_options(ctx: ActorDep) -> LaborOptions:
    settings, currency = rules(), ctx.user.base_currency
    return LaborOptions(
        kinds=list(settings.projects.kinds),
        genders=list(settings.workers.genders),
        methods=list(settings.payments.methods),
        units=settings.wages.units,
        max_daily_minor=to_minor(settings.wages.max_daily, currency),
        photo_max_bytes=settings.files.photo_max_bytes,
        id_max_bytes=settings.files.id_max_bytes,
        photo_side_px=settings.files.photo_side_px,
        id_side_px=settings.files.id_side_px,
        max_report_days=settings.reports.max_days,
    )


@router.get(
    "/overview", summary="Today across my open projects", responses=error_responses(401, 403)
)
async def labor_overview(ctx: ActorDep) -> OverviewOut:
    return await Overview(ctx).today()


@router.get(
    "/audit",
    summary="Every change to my projects, newest first",
    responses=error_responses(401, 403, 422),
)
async def labor_audit(
    ctx: ActorDep, params: PageParamsDep, filters: AuditFiltersDep
) -> Page[LaborAuditOut]:
    return await audit_page(ctx, params, filters)


# ---------------------------------------------------------------------- projects


@router.post(
    "/projects", status_code=status.HTTP_201_CREATED, responses=error_responses(401, 403, 409, 422)
)
async def add_project(body: ProjectIn, service: ProjectsDep) -> LaborProjectOut:
    return LaborProjectOut.of(await service.add(body))


@router.get(
    "/projects", summary="My projects, open ones first", responses=error_responses(401, 403, 422)
)
async def list_projects(
    service: ProjectsDep, params: PageParamsDep, state: ProjectStatus | None = None, q: Words = None
) -> Page[LaborProjectOut]:
    found, total = await service.page(params, state, q)
    return Page[LaborProjectOut].build([LaborProjectOut.of(item) for item in found], params, total)


@router.get("/projects/{project_id}", responses=error_responses(401, 403, 404))
async def read_project(project_id: str, service: ProjectsDep) -> LaborProjectOut:
    return LaborProjectOut.of(await service.get(project_id))


@router.patch("/projects/{project_id}", responses=error_responses(401, 403, 404, 422))
async def change_project(project_id: str, body: ProjectIn, service: ProjectsDep) -> LaborProjectOut:
    return LaborProjectOut.of(await service.change(project_id, body))


@router.post("/projects/{project_id}/close", responses=error_responses(401, 403, 404, 409, 422))
async def close_project(project_id: str, body: StatusIn, service: ProjectsDep) -> LaborProjectOut:
    project = await service.set_status(project_id, ProjectStatus.CLOSED, body.note)
    return LaborProjectOut.of(project)


@router.post("/projects/{project_id}/reopen", responses=error_responses(401, 403, 404, 409, 422))
async def reopen_project(project_id: str, body: StatusIn, service: ProjectsDep) -> LaborProjectOut:
    return LaborProjectOut.of(await service.set_status(project_id, ProjectStatus.OPEN, body.note))


@router.put(
    "/projects/{project_id}/rates",
    summary="The usual daily wages for new workers (workers already in keep theirs)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def set_rates(project_id: str, body: RatesIn, service: ProjectsDep) -> LaborProjectOut:
    return LaborProjectOut.of(await service.set_rates(project_id, body))


@router.post(
    "/projects/{project_id}/workers",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def add_worker(project_id: str, body: WorkerIn, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.add(project_id, body))


@router.get("/projects/{project_id}/workers", responses=error_responses(401, 403, 404, 422))
async def list_workers(
    project_id: str,
    service: WorkersDep,
    params: PageParamsDep,
    active: bool | None = None,
    q: Words = None,
) -> Page[LaborWorkerOut]:
    found, total = await service.page(project_id, params, active, q)
    return Page[LaborWorkerOut].build([LaborWorkerOut.of(item) for item in found], params, total)


@router.get(
    "/projects/{project_id}/attendance",
    summary="A day's attendance",
    responses=error_responses(401, 403, 404, 422),
)
async def read_day(project_id: str, day: Day, ctx: ActorDep) -> LaborDayOut:
    return day_out(await Attendance(ctx).sheet(project_id, day))


@router.put(
    "/projects/{project_id}/attendance",
    summary="Mark workers full, half or absent for a day (a mark already there is changed)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def mark_day(project_id: str, day: Day, body: MarksIn, ctx: ActorDep) -> LaborDayOut:
    return day_out(await Attendance(ctx).mark(project_id, day, body.marks))


@router.post(
    "/projects/{project_id}/payments",
    status_code=status.HTTP_201_CREATED,
    summary="Record money paid to a worker (sent twice with one Idempotency-Key, kept once)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def add_payment(
    project_id: str, body: PaymentIn, ctx: ActorDep, response: Response, key: IdempotencyKey = None
) -> LaborPaymentOut:
    payment = answer_once(response, await Payments(ctx).add(project_id, body, key))
    return LaborPaymentOut.of(payment)


@router.get("/projects/{project_id}/payments", responses=error_responses(401, 403, 404, 422))
async def list_payments(
    project_id: str,
    ctx: ActorDep,
    params: PageParamsDep,
    worker_id: str | None = None,
    start: Annotated[dt.date | None, Query(alias="from")] = None,
    end: Annotated[dt.date | None, Query(alias="to")] = None,
) -> Page[LaborPaymentOut]:
    found, total = await Payments(ctx).page(project_id, params, worker_id, start, end)
    return Page[LaborPaymentOut].build([LaborPaymentOut.of(item) for item in found], params, total)


@router.post(
    "/projects/{project_id}/attendance/read",
    summary="Read marks said or typed into a draft to check (mark-attendance); nothing is saved",
    dependencies=[Depends(rate_limit("agent_run"))],
    responses=error_responses(401, 403, 404, 422, 429),
)
async def read_marks_said(
    project_id: str,
    body: ReadMarksIn,
    ctx: ActorDep,
    day: Annotated[dt.date | None, Query(description="The page's day; today if left out")] = None,
) -> MarksReadOut:
    await Projects(ctx).get(project_id)  # theirs, or not found
    context = {"input_type": body.input_type, "project_id": project_id}
    if day:
        context["day"] = day.isoformat()
    locale = body.locale or ctx.user.locale
    run = await run_skill(ctx, MARKS_SKILL, body.text, locale=locale, context=context)
    return marks_read_out(run, MARKS_TOOL)


@router.get(
    "/projects/{project_id}/report",
    summary="Earned and paid, by day, week or month and by worker, with what is owed",
    responses=error_responses(401, 403, 404, 422),
)
async def project_report(
    project_id: str,
    ctx: ActorDep,
    start: Annotated[dt.date, Query(alias="from")],
    end: Annotated[dt.date, Query(alias="to")],
    group: Grouping = Grouping.DAY,
) -> LaborReportOut:
    return report_out(await Reports(ctx).project_report(project_id, start, end, group))


@router.get(
    "/projects/{project_id}/report.csv",
    summary="The report as a CSV file: its figures, then each day marked and each payment",
    response_class=Response,
    responses={**CSV_RESPONSE, **error_responses(401, 403, 404, 422, 429)},
    dependencies=[Depends(rate_limit("export"))],
)
async def project_report_file(
    project_id: str,
    ctx: ActorDep,
    start: Annotated[dt.date, Query(alias="from")],
    end: Annotated[dt.date, Query(alias="to")],
    group: Grouping = Grouping.DAY,
) -> Response:
    text = await report_file(ctx, project_id, start, end, group)
    return Response(text, media_type=CSV_MEDIA_TYPE, headers=attachment(filename(start, end)))


# ---------------------------------------------------------------------- workers


@router.get("/workers/{worker_id}", responses=error_responses(401, 403, 404))
async def read_worker(worker_id: str, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.get(worker_id))


@router.patch("/workers/{worker_id}", responses=error_responses(401, 403, 404, 409, 422))
async def change_worker(worker_id: str, body: WorkerIn, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.change(worker_id, body))


@router.put(
    "/workers/{worker_id}/wage",
    summary="A new daily wage from a day on (past attendance keeps its rate)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def set_wage(worker_id: str, body: WageIn, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.set_wage(worker_id, body))


@router.get(
    "/workers/{worker_id}/wages",
    summary="A worker's wage history, newest first",
    responses=error_responses(401, 403, 404),
)
async def wage_history(worker_id: str, service: WorkersDep) -> list[LaborWageOut]:
    return [LaborWageOut.of(item) for item in await service.wage_history(worker_id)]


@router.get(
    "/workers/{worker_id}/history",
    summary="A worker's days and payments between two days, newest first",
    responses=error_responses(401, 403, 404, 422),
)
async def worker_history(
    worker_id: str,
    ctx: ActorDep,
    start: Annotated[dt.date, Query(alias="from")],
    end: Annotated[dt.date, Query(alias="to")],
) -> LaborWorkerHistoryOut:
    return history_out(await Reports(ctx).worker_history(worker_id, start, end))


@router.put("/workers/{worker_id}/active", responses=error_responses(401, 403, 404, 409, 422))
async def set_active(worker_id: str, body: ActiveIn, service: WorkersDep) -> LaborWorkerOut:
    worker = await service.set_active(worker_id, active=body.active, note=body.note)
    return LaborWorkerOut.of(worker)


@router.put(
    "/workers/{worker_id}/photo",
    summary="A worker's photo: the body, a JPEG, PNG or WebP",
    responses=PICTURE,
    openapi_extra=IMAGE_BODY,
)
async def put_photo(worker_id: str, request: Request, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.put_picture(worker_id, "photo", request))


@router.get(
    "/workers/{worker_id}/photo",
    summary="A worker's photo, only to their owner",
    response_class=Response,
    responses=error_responses(401, 403, 404),
)
async def read_photo(worker_id: str, service: WorkersDep) -> Response:
    return file_response(await service.picture(worker_id, "photo"))


@router.delete("/workers/{worker_id}/photo", responses=error_responses(401, 403, 404, 409))
async def remove_photo(worker_id: str, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.remove_picture(worker_id, "photo"))


@router.put(
    "/workers/{worker_id}/id-card",
    summary="A picture of a worker's ID, if the owner keeps one: a JPEG, PNG or WebP",
    responses=PICTURE,
    openapi_extra=IMAGE_BODY,
)
async def put_id_card(worker_id: str, request: Request, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.put_picture(worker_id, "id_card", request))


@router.get(
    "/workers/{worker_id}/id-card",
    summary="The picture of a worker's ID, only to their owner",
    response_class=Response,
    responses=error_responses(401, 403, 404),
)
async def read_id_card(worker_id: str, service: WorkersDep) -> Response:
    return file_response(await service.picture(worker_id, "id_card"))


@router.delete("/workers/{worker_id}/id-card", responses=error_responses(401, 403, 404, 409))
async def remove_id_card(worker_id: str, service: WorkersDep) -> LaborWorkerOut:
    return LaborWorkerOut.of(await service.remove_picture(worker_id, "id_card"))


# ---------------------------------------------------------------------- payments


@router.post(
    "/payments/{payment_id}/void",
    summary="Void a mistaken payment, with the reason (it stays in the history)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def void_payment(payment_id: str, body: VoidIn, ctx: ActorDep) -> LaborPaymentOut:
    return LaborPaymentOut.of(await Payments(ctx).void(payment_id, body.reason))
