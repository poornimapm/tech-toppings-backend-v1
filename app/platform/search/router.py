"""``/v1/search``: the signed-in user's own things, from every module they use."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.errors import error_responses
from app.core.rate_limit import rate_limit
from app.platform.context import ActorDep
from app.platform.modules.deps import ModuleServiceDep
from app.platform.search.service import SearchOut, search_all

router = APIRouter(prefix="/search", tags=["search"])


@router.get(
    "",
    summary="Find my own things in every module I use",
    responses=error_responses(401, 429),
    dependencies=[Depends(rate_limit("search"))],
)
async def search_everything(
    ctx: ActorDep,
    modules: ModuleServiceDep,
    q: Annotated[str, Query(description="What to look for: any case, part of a word")],
) -> SearchOut:
    # A module's search only while that module is available to me, as for its widgets.
    usable = await modules.usable_owners()
    mine = [item for item in ctx.state.searchers if usable(item.owner)]
    return await search_all(ctx, q, mine)
