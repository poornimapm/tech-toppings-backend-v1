"""``/v1/me``: the signed-in user's own profile."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter

from app.core.errors import error_responses
from app.platform.auth.deps import CurrentUserAllowingPasswordChange
from app.platform.context import ActorDep
from app.platform.reminders.service import Reminders
from app.platform.users.repository import UserRepository
from app.platform.users.schemas import UserOut, UserUpdate
from app.platform.users.service import UserService

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", summary="My profile", responses=error_responses(401))
async def read_me(user: CurrentUserAllowingPasswordChange) -> UserOut:
    return UserOut.from_user(user)


@router.patch(
    "",
    summary="Update my profile",
    responses=error_responses(401, 403),
)
async def update_me(changes: UserUpdate, ctx: ActorDep) -> UserOut:
    users = UserService(UserRepository(ctx.clock), ctx.clock)
    updated = await users.update_profile(ctx.user, changes)
    if updated.timezone != ctx.user.timezone:  # reminders stay at the send hour, local time
        await Reminders(replace(ctx, user=updated)).rezone(ctx.user.timezone)
    return UserOut.from_user(updated)
