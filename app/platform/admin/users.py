from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.deps import SettingsDep
from app.core.errors import error_responses
from app.core.pagination import Page, page_params
from app.platform.admin.accounts import Accounts
from app.platform.admin.deps import AdminActionDep
from app.platform.users.models import Role, User

router = APIRouter(prefix="/users")


class AccountState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ALL = "all"


class AdminUserOut(BaseModel):
    id: str
    email: str
    name: str
    role: Role
    created_at: dt.datetime
    last_login_at: dt.datetime | None
    disabled: bool
    must_change_password: bool
    two_factor: bool
    locked: bool
    providers: list[str]

    @classmethod
    def of(cls, user: User, now: dt.datetime) -> AdminUserOut:
        locked = user.locked_until is not None and _aware(user.locked_until) > now
        return cls(
            id=str(user.id),
            email=user.email,
            name=user.name,
            role=user.role,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            disabled=user.disabled_at is not None,
            must_change_password=user.must_change_password,
            two_factor=user.two_factor,
            locked=locked,
            providers=[provider.provider for provider in user.auth_providers],
        )


class AdminUserDetail(AdminUserOut):
    live_sessions: int


class UserQuery(BaseModel):
    q: str | None = Field(default=None, max_length=80, description="Words in the email or name")
    role: Role | None = None
    state: AccountState = AccountState.ACTIVE
    page: int = Field(default=1, ge=1)
    page_size: int | None = Field(default=None, ge=1)


class AdminUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    disabled: bool | None = None

    @model_validator(mode="after")
    def something(self) -> AdminUserPatch:
        if self.role is None and self.disabled is None:
            raise ValueError("send a role, disabled, or both")
        return self


class RevokedOut(BaseModel):
    sessions: int


class OneTimePasswordOut(BaseModel):
    """Shown once and never stored in the clear: give it to the user, who must change it."""

    one_time_password: str


def _aware(moment: dt.datetime) -> dt.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def accounts(action: AdminActionDep) -> Accounts:
    return Accounts(action)


AccountsDep = Annotated[Accounts, Depends(accounts)]


@router.get("", summary="Accounts, newest first", responses=error_responses(401, 403, 422))
async def list_users(
    service: AccountsDep, settings: SettingsDep, query: Annotated[UserQuery, Query()]
) -> Page[AdminUserOut]:
    params = page_params(settings, query.page, query.page_size)
    found, total = await service.list(params, query.q, query.role, query.state)
    now = service.ctx.clock.now()
    return Page[AdminUserOut].build([AdminUserOut.of(user, now) for user in found], params, total)


@router.get("/{user_id}", summary="One account", responses=error_responses(401, 403, 404))
async def read_user(user_id: PydanticObjectId, service: AccountsDep) -> AdminUserDetail:
    user = await service.one(user_id)
    out = AdminUserOut.of(user, service.ctx.clock.now())
    return AdminUserDetail(**out.model_dump(), live_sessions=await service.live_sessions(user_id))


@router.patch(
    "/{user_id}",
    summary="Change an account's role, or disable or enable it (audited)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def change_user(
    user_id: PydanticObjectId, body: AdminUserPatch, service: AccountsDep
) -> AdminUserOut:
    user = await service.change(user_id, body.role, body.disabled)
    return AdminUserOut.of(user, service.ctx.clock.now())


@router.post(
    "/{user_id}/revoke-sessions",
    summary="Sign an account out everywhere (audited)",
    responses=error_responses(401, 403, 404, 409),
)
async def revoke_user_sessions(user_id: PydanticObjectId, service: AccountsDep) -> RevokedOut:
    return RevokedOut(sessions=await service.revoke_sessions(user_id))


@router.post(
    "/{user_id}/reset-password",
    summary="Reset an account's password to a one-time one, returned once; clears 2FA (audited)",
    responses=error_responses(401, 403, 404, 409),
)
async def reset_user_password(
    user_id: PydanticObjectId, service: AccountsDep
) -> OneTimePasswordOut:
    return OneTimePasswordOut(one_time_password=await service.reset_password(user_id))
