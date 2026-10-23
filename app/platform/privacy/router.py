"""``/v1/me/export`` and ``DELETE /v1/me``: a person's right to their data, and to leave."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import StreamingResponse

from app.core.deps import SettingsDep
from app.core.errors import error_responses
from app.core.rate_limit import rate_limit
from app.platform.auth.deps import ClientInfoDep, ReauthDep, TwoFactorDep
from app.platform.auth.router import clear_refresh_cookie
from app.platform.context import ActorDep
from app.platform.privacy.service import AccountData, AccountDeleteIn

router = APIRouter(prefix="/me", tags=["me"])

JSON_FILE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/json": {}}, "description": "Everything, as one JSON file"}
}


@router.get(
    "/export",
    summary="All my data, as one JSON file",
    response_class=StreamingResponse,
    responses={**JSON_FILE, **error_responses(401, 403, 429)},
    dependencies=[Depends(rate_limit("export"))],
)
async def export_my_data(ctx: ActorDep, client: ClientInfoDep) -> StreamingResponse:
    data = AccountData(ctx)
    await data.record_export(client.ip)
    return StreamingResponse(
        data.export(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{data.filename()}"'},
    )


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my account and everything in it, for good",
    responses=error_responses(401, 403, 409, 422, 429, 503),
    dependencies=[Depends(rate_limit("auth_password_change"))],
)
async def delete_me(
    body: AccountDeleteIn,
    ctx: ActorDep,
    two_factor: TwoFactorDep,
    reauth: ReauthDep,
    client: ClientInfoDep,
    response: Response,
    settings: SettingsDep,
) -> None:
    data = AccountData(ctx)
    await data.confirm(body, reauth, two_factor, client.ip)
    await data.erase(client.ip)
    clear_refresh_cookie(response, settings.auth)
