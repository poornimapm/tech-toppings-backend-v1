"""A project's report as a CSV file (D5, ADR-0024): the same figures as the report page, then
each day marked and each payment, so the file stands as the period's record. One header and a
``section`` column, like the expenses monthly file:

- ``total``: the period's sums (``start`` is its first day);
- ``period``: each day, week (from Monday) or month (from the 1st);
- ``worker``: each worker's sums, and what they are owed up to the period's end;
- ``mark``: a day marked (``status`` full, half or absent; ``days`` as paid; ``earned``);
- ``payment``: money paid (voided payments are left out, as in the sums).

Columns keep stable English names; amounts are in the currency's main unit ("150.51")."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.modules.labor.models import LaborAttendance, LaborPayment, LaborWorker
from app.modules.labor.reports import Grouping, Report, Reports, Totals
from app.platform import ActorContext, csv_text, major_text, safe_text

COLUMNS = (
    "section",
    "start",
    "worker_code",
    "worker",
    "status",
    "full",
    "half",
    "absent",
    "days",
    "earned",
    "paid",
    "owed",
    "owed_to_date",
)


def _days(value: float) -> str:
    return f"{value:g}"


class _Rows:
    def __init__(self, report: Report) -> None:
        self.report = report

    def money(self, minor: int) -> str:
        return major_text(minor, self.report.currency)

    def who(self, worker_id: object) -> list[str]:
        worker: LaborWorker | None = self.report.names.get(str(worker_id))
        return [worker.code, safe_text(worker.name)] if worker else ["", ""]

    def sums(self, section: str, start: str, who: list[str], totals: Totals) -> list[str]:
        return [
            section,
            start,
            *who,
            "",
            str(totals.full),
            str(totals.half),
            str(totals.absent),
            _days(totals.days),
            self.money(totals.earned_minor),
            self.money(totals.paid_minor),
            self.money(totals.owed_minor),
        ]

    def all(
        self, marks: Sequence[LaborAttendance], payments: Sequence[LaborPayment]
    ) -> list[list[str]]:
        report = self.report
        rows = [[*self.sums("total", report.start.isoformat(), ["", ""], report.total), ""]]
        rows += [
            [*self.sums("period", start.isoformat(), ["", ""], totals), ""]
            for start, totals in report.periods
        ]
        workers = sorted(set(report.workers) | set(report.to_date), key=self.who)
        rows += [
            [
                *self.sums("worker", "", self.who(worker), report.workers.get(worker, Totals())),
                self.money(report.to_date.get(worker, Totals()).owed_minor),
            ]
            for worker in workers
        ]
        blank = ["", "", ""]  # full, half and absent: a single mark says it in ``status``
        rows += [
            ["mark", mark.day, *self.who(mark.worker_id), mark.status.value, *blank,
             _days(mark.units), self.money(mark.amount_minor), "", "", ""]
            for mark in sorted(marks, key=lambda mark: (mark.day, self.who(mark.worker_id)))
        ]  # fmt: skip
        rows += [
            ["payment", payment.day, *self.who(payment.worker_id), "", *blank, "", "",
             self.money(payment.amount_minor), "", ""]
            for payment in payments
        ]  # fmt: skip
        return rows


def filename(start: date, end: date) -> str:
    """ASCII and dates only: a project's name may be Tamil or Hindi."""
    return f"labor-report-{start.isoformat()}-to-{end.isoformat()}.csv"


async def report_file(
    ctx: ActorContext, project_id: str, start: date, end: date, grouping: Grouping
) -> str:
    """The file's text; a period too long, or with more rows than a file takes, is refused
    before any of it is made."""
    reports = Reports(ctx)
    marks, payments = await reports.entries(project_id, start, end)
    report = await reports.project_report(project_id, start, end, grouping)
    return csv_text(COLUMNS, _Rows(report).all(marks, payments))
