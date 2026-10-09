"""``/v1/auth/*`` and ``/v1/me/sessions``.

The access token goes back in the JSON body (kept in memory by the client); the refresh
token is only ever set as an httpOnly cookie scoped to the auth path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.clock import Clock
from app.core.config import AuthSettings
from app.core.deps import ClockDep, SettingsDep
from app.core.errors import error_responses
from app.core.pagination import Page, PageParamsDep, parse_sort
from app.core.rate_limit import rate_limit
from app.platform.auth.deps import (
    AuthServiceDep,
    ClientInfoDep,
    CurrentClaims,
    CurrentUserAllowingPasswordChange,
    UserScope,
    require_csrf_header,
)
from app.platform.auth.repository import SessionRepository
from app.platform.auth.schemas import (
    AuthConfigOut,
    GoogleSignInIn,
    LoginIn,
    PasswordChangeIn,
    RegisterIn,
    SessionOut,
    TokenOut,
)
from app.platform.auth.service import AuthResult
from app.platform.users.schemas import UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
sessions_router = APIRouter(prefix="/me/sessions", tags=["me"])


def _set_refresh_cookie(
    response: Response, result: AuthResult, auth: AuthSettings, clock: Clock
) -> None:
    max_age = int((result.refresh.session.expires_at - clock.now()).total_seconds())
    response.set_cookie(
        key=auth.refresh_cookie_name,
        value=result.refresh.refresh_token,
        max_age=max(max_age, 0),
        path=auth.refresh_cookie_path,
        secure=auth.refresh_cookie_secure,
        httponly=True,
        samesite=auth.refresh_cookie_samesite,
    )


def _clear_refresh_cookie(response: Response, auth: AuthSettings) -> None:
    response.delete_cookie(
        key=auth.refresh_cookie_name,
        path=auth.refresh_cookie_path,
        secure=auth.refresh_cookie_secure,
        httponly=True,
        samesite=auth.refresh_cookie_samesite,
    )


def _respond(
    response: Response, result: AuthResult, settings: SettingsDep, clock: Clock
) -> TokenOut:
    _set_refresh_cookie(response, result, settings.auth, clock)
    return TokenOut(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user=UserOut.from_user(result.user),
    )


@router.get("/config", summary="Public sign-in configuration")
async def auth_config(service: AuthServiceDep) -> AuthConfigOut:
    return service.config()


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("auth_register"))],
    responses=error_responses(403, 409, 429),
)
async def register(
    body: RegisterIn,
    response: Response,
    service: AuthServiceDep,
    client: ClientInfoDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenOut:
    return _respond(response, await service.register(body, client), settings, clock)


@router.post(
    "/login",
    dependencies=[Depends(rate_limit("auth_login"))],
    responses=error_responses(401, 403, 429),
)
async def login(
    body: LoginIn,
    response: Response,
    service: AuthServiceDep,
    client: ClientInfoDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenOut:
    return _respond(response, await service.login(body, client), settings, clock)


@router.post(
    "/refresh",
    dependencies=[Depends(rate_limit("auth_refresh")), Depends(require_csrf_header)],
    responses=error_responses(401, 403, 409, 429),
)
async def refresh(
    request: Request,
    response: Response,
    service: AuthServiceDep,
    client: ClientInfoDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenOut:
    token = request.cookies.get(settings.auth.refresh_cookie_name)
    return _respond(response, await service.refresh(token, client), settings, clock)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf_header)],
    responses=error_responses(403),
)
async def logout(
    request: Request, response: Response, service: AuthServiceDep, settings: SettingsDep
) -> None:
    await service.logout(request.cookies.get(settings.auth.refresh_cookie_name))
    _clear_refresh_cookie(response, settings.auth)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, responses=error_responses(401))
async def logout_all(
    user: CurrentUserAllowingPasswordChange,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
) -> None:
    await service.logout_all(user)
    _clear_refresh_cookie(response, settings.auth)


@router.post(
    "/password/change",
    dependencies=[Depends(rate_limit("auth_password_change"))],
    responses=error_responses(401, 422, 429),
)
async def change_password(
    body: PasswordChangeIn,
    user: CurrentUserAllowingPasswordChange,
    response: Response,
    service: AuthServiceDep,
    client: ClientInfoDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenOut:
    return _respond(response, await service.change_password(user, body, client), settings, clock)


@router.post(
    "/google",
    dependencies=[Depends(rate_limit("auth_login"))],
    responses=error_responses(401, 403, 404, 429),
)
async def google_sign_in(
    body: GoogleSignInIn,
    response: Response,
    service: AuthServiceDep,
    client: ClientInfoDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenOut:
    return _respond(response, await service.google_sign_in(body.id_token, client), settings, clock)


# ---------------------------------------------------------------- /v1/me/sessions

SESSION_SORT_FIELDS = ("last_used_at", "created_at")


@sessions_router.get("", summary="Signed-in devices", responses=error_responses(401))
async def list_sessions(
    scope: UserScope,
    claims: CurrentClaims,
    params: PageParamsDep,
    clock: ClockDep,
    sort: Annotated[str | None, Query(description="e.g. -last_used_at")] = None,
    active: Annotated[bool, Query(description="Only sessions that can still be used")] = True,
) -> Page[SessionOut]:
    filters: dict[str, object] = {}
    if active:
        filters = {"revoked_at": None, "expires_at": {"$gt": clock.now()}}
    sessions, total = await SessionRepository(scope, clock).list(
        params, parse_sort(sort, SESSION_SORT_FIELDS, "-last_used_at"), filters
    )
    items = [
        SessionOut(
            id=str(session.id),
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            expires_at=session.expires_at,
            user_agent=session.user_agent,
            ip=session.ip,
            current=session.id == claims.session_id,
        )
        for session in sessions
    ]
    return Page[SessionOut].build(items, params, total)


@sessions_router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out one device",
    responses=error_responses(401, 404),
)
async def revoke_session(session_id: str, scope: UserScope, clock: ClockDep) -> None:
    await SessionRepository(scope, clock).revoke(session_id)
