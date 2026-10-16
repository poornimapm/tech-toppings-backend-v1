from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.modules.expenses.features.reports.schemas import MONTH, MonthlyReportOut
from app.modules.expenses.features.reports.service import Reports
from app.platform import ActorDep, error_responses, rate_limit

router = APIRouter(prefix="/reports")

Month = Annotated[str | None, Query(pattern=MONTH, description="YYYY-MM; default this month")]
CSV_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {}}, "description": "UTF-8 CSV: a row per figure"}
}


@router.get(
    "/monthly",
    summary="A month on one page: totals against the month before, where it went, the biggest",
    responses=error_responses(401, 403, 422),
)
async def monthly_report(ctx: ActorDep, month: Month = None) -> MonthlyReportOut:
    return await Reports(ctx).monthly(month)


@router.get(
    "/monthly.csv",
    summary="The monthly report's figures as a CSV file",
    response_class=Response,
    responses={**CSV_RESPONSE, **error_responses(401, 403, 422, 429)},
    dependencies=[Depends(rate_limit("export"))],
)
async def monthly_report_csv(ctx: ActorDep, month: Month = None) -> Response:
    reports = Reports(ctx)
    name = f"expenses-report-{reports.month_of(month)}.csv"
    return Response(
        await reports.csv_text(month),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
