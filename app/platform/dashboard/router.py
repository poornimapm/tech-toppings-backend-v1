"""``/v1/dashboard``: the signed-in user's own figures for a period."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.errors import error_responses
from app.platform.context import ActorDep
from app.platform.dashboard.service import DashboardOut, build_dashboard, period_for
from app.platform.modules.deps import ModuleServiceDep

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

FromDate = Annotated[date | None, Query(alias="from", description="First day (default: recent)")]
ToDate = Annotated[
    date | None, Query(alias="to", description="Last day, inclusive (default: today)")
]


@router.get("", summary="My dashboard", responses=error_responses(401, 422))
async def read_dashboard(
    ctx: ActorDep, modules: ModuleServiceDep, start: FromDate = None, end: ToDate = None
) -> DashboardOut:
    module_keys = {module.manifest.key for module in ctx.state.modules}
    available = {item.key for item in await modules.arranged() if item.available}
    # Platform widgets always; a module's widgets only while that module is available to me.
    mine = [w for w in ctx.state.widgets if w.owner not in module_keys or w.owner in available]
    return await build_dashboard(ctx, period_for(ctx, start, end), mine)
