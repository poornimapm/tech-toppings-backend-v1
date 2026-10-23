"""``/v1/auth/totp``: turning two-step sign-in on and off, and the recovery codes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.errors import error_responses
from app.core.rate_limit import rate_limit
from app.platform.auth.deps import ClientInfoDep, CurrentUser, ReauthDep, TwoFactorDep
from app.platform.auth.schemas import (
    RecoveryCodesOut,
    TwoFactorCodeIn,
    TwoFactorConfirmIn,
    TwoFactorOut,
    TwoFactorSetupOut,
)

router = APIRouter(
    prefix="/auth/totp",
    tags=["auth"],
    dependencies=[Depends(rate_limit("auth_password_change"))],
)


@router.get("", summary="Whether two-step sign-in is on", responses=error_responses(401, 429))
async def read_two_factor(user: CurrentUser, two_factor: TwoFactorDep) -> TwoFactorOut:
    return TwoFactorOut(
        available=two_factor.available,
        enabled=user.two_factor,
        enabled_at=user.totp_enabled_at,
        recovery_codes_left=len(user.recovery_codes) if user.two_factor else 0,
    )


@router.post(
    "/setup",
    summary="A new secret to add to an authenticator app",
    responses=error_responses(401, 409, 429, 503),
)
async def setup_two_factor(user: CurrentUser, two_factor: TwoFactorDep) -> TwoFactorSetupOut:
    secret, uri = await two_factor.setup(user)
    return TwoFactorSetupOut(secret=secret, otpauth_uri=uri)


@router.post(
    "/enable",
    summary="Turn it on (password and a code from the app); returns the recovery codes once",
    responses=error_responses(401, 409, 422, 429, 503),
)
async def enable_two_factor(
    body: TwoFactorConfirmIn,
    user: CurrentUser,
    two_factor: TwoFactorDep,
    reauth: ReauthDep,
    client: ClientInfoDep,
) -> RecoveryCodesOut:
    await reauth.confirm(user, password=body.password, google_id_token=body.google_id_token)
    return RecoveryCodesOut(recovery_codes=await two_factor.enable(user, body.code, client.ip))


@router.post(
    "/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Turn it off (password and a code)",
    responses=error_responses(401, 409, 422, 429, 503),
)
async def disable_two_factor(
    body: TwoFactorConfirmIn,
    user: CurrentUser,
    two_factor: TwoFactorDep,
    reauth: ReauthDep,
    client: ClientInfoDep,
) -> None:
    await reauth.confirm(user, password=body.password, google_id_token=body.google_id_token)
    await two_factor.disable(user, body.code, client.ip)


@router.post(
    "/recovery-codes",
    summary="New recovery codes in place of the old ones (a code as proof)",
    responses=error_responses(401, 409, 422, 429, 503),
)
async def renew_recovery_codes(
    body: TwoFactorCodeIn, user: CurrentUser, two_factor: TwoFactorDep, client: ClientInfoDep
) -> RecoveryCodesOut:
    codes = await two_factor.renew_recovery_codes(user, body.code, client.ip)
    return RecoveryCodesOut(recovery_codes=codes)
