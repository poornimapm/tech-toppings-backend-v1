from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.deps import ClockDep, SettingsDep
from app.core.errors import UnauthenticatedError, error_responses
from app.core.repository import Scope
from app.platform.auth.deps import AdminUser
from app.platform.context import ActorContext
from app.platform.dashboard.router import FromDate, ToDate
from app.platform.dashboard.service import DashboardOut, build_dashboard, period_for

router = APIRouter(prefix="/admin", tags=["admin"])


def admin_context(
    request: Request, admin: AdminUser, clock: ClockDep, settings: SettingsDep
) -> ActorContext:
    """The admin acting across all users: plugin code sees an unrestricted scope."""
    if admin.id is None:  # pragma: no cover - persisted users always have an id
        raise UnauthenticatedError
    return ActorContext(
        user=admin,
        scope=Scope.unrestricted(admin.id),
        clock=clock,
        settings=settings,
        state=request.app.state,
    )


AdminDep = Annotated[ActorContext, Depends(admin_context)]


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
