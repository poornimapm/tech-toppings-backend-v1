from __future__ import annotations

from fastapi import APIRouter

from app.core.errors import error_responses
from app.platform.admin.deps import AdminDep
from app.platform.dashboard.router import FromDate, ToDate
from app.platform.dashboard.service import DashboardOut, build_dashboard, period_for

router = APIRouter()


@router.get(
    "/dashboard", summary="Dashboard across all users", responses=error_responses(401, 403, 422)
)
async def read_all_users_dashboard(
    ctx: AdminDep, start: FromDate = None, end: ToDate = None
) -> DashboardOut:
    # Aggregates only: list widgets show individual records (someone's latest entries), which
    # an admin has no reason to read here.
    aggregates = [item for item in ctx.state.widgets if item.spec.kind != "list"]
    return await build_dashboard(ctx, period_for(ctx, start, end), aggregates)
