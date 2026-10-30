"""The services' results as the API gives them."""

from __future__ import annotations

from app.modules.labor.attendance import DaySheet
from app.modules.labor.reports import History, Report, Totals
from app.modules.labor.schemas import (
    AttendanceDraftIn,
    LaborDayMarkOut,
    LaborDayOut,
    LaborMarkOut,
    LaborPaymentOut,
    LaborPeriodOut,
    LaborReportOut,
    LaborTotalsOut,
    LaborWorkerHistoryOut,
    LaborWorkerOut,
    LaborWorkerTotalsOut,
    MarksReadOut,
)
from app.platform import AgentRun


def day_out(sheet: DaySheet) -> LaborDayOut:
    return LaborDayOut(
        day=sheet.day,
        workers=[LaborWorkerOut.of(worker) for worker in sheet.workers],
        marks=[LaborMarkOut.of(mark) for mark in sheet.marks.values()],
        counts=sheet.counts(),
        total_minor=sheet.total_minor,
    )


def _totals(totals: Totals) -> LaborTotalsOut:
    return LaborTotalsOut(
        full=totals.full,
        half=totals.half,
        absent=totals.absent,
        days=totals.days,
        earned_minor=totals.earned_minor,
        paid_minor=totals.paid_minor,
        owed_minor=totals.owed_minor,
    )


def _worker_totals(report: Report, worker_id: str) -> LaborWorkerTotalsOut:
    worker = report.names.get(worker_id)
    to_date = report.to_date.get(worker_id, Totals())
    return LaborWorkerTotalsOut(
        worker_id=worker_id,
        code=worker.code if worker else "",
        name=worker.name if worker else "",
        earned_to_date_minor=to_date.earned_minor,
        paid_to_date_minor=to_date.paid_minor,
        owed_to_date_minor=to_date.owed_minor,
        **_totals(report.workers.get(worker_id, Totals())).model_dump(),
    )


def report_out(report: Report) -> LaborReportOut:
    """Every worker with days or money in the report, by worker number."""
    workers = set(report.workers) | set(report.to_date)
    rows = [_worker_totals(report, worker) for worker in workers]
    return LaborReportOut(
        start=report.start,
        end=report.end,
        grouping=report.grouping.value,
        periods=[
            LaborPeriodOut(start=start, **_totals(totals).model_dump())
            for start, totals in report.periods
        ],
        workers=sorted(rows, key=lambda row: (row.code, row.worker_id)),
        total=_totals(report.total),
    )


def marks_read_out(run: AgentRun, tool: str) -> MarksReadOut:
    """A mark-attendance run as the page takes it: its words, and the draft it handed over."""
    action = next((action for action in run.client_actions if action.tool == tool), None)
    return MarksReadOut(
        status=run.status.value,
        mode=run.mode.value if run.mode else None,
        reason=run.reason.value if run.reason else None,
        message=run.message,
        message_code=run.message_code,
        message_params=run.message_params,
        draft=AttendanceDraftIn.model_validate(action.arguments["draft"]) if action else None,
    )


def history_out(history: History) -> LaborWorkerHistoryOut:
    return LaborWorkerHistoryOut(
        worker=LaborWorkerOut.of(history.worker),
        start=history.start,
        end=history.end,
        days=[LaborDayMarkOut.of(mark) for mark in history.marks],
        payments=[LaborPaymentOut.of(payment) for payment in history.payments],
        earned_minor=sum(mark.amount_minor for mark in history.marks),
        paid_minor=sum(p.amount_minor for p in history.payments if p.voided_at is None),
    )
