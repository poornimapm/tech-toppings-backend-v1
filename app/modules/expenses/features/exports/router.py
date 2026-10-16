from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.modules.expenses.features.entries.schemas import EntryFilters
from app.modules.expenses.features.exports.service import Exports
from app.platform import ActorDep, error_responses, rate_limit

router = APIRouter()

CSV_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {}}, "description": "UTF-8 CSV, newest first"}
}


@router.get(
    "/export.csv",
    summary="My entries as a CSV file, with the entries list's filters",
    response_class=StreamingResponse,
    responses={**CSV_RESPONSE, **error_responses(401, 403, 422, 429)},
    dependencies=[Depends(rate_limit("export"))],
)
async def export_entries(
    ctx: ActorDep, filters: Annotated[EntryFilters, Query()]
) -> StreamingResponse:
    exports = Exports(ctx)
    await exports.check(filters)
    return StreamingResponse(
        exports.lines(filters),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{exports.filename(filters)}"'},
    )
