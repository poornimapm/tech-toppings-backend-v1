"""``/v1/me``: the signed-in user's own profile."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import ClockDep
from app.core.errors import error_responses
from app.platform.auth.deps import CurrentUser, CurrentUserAllowingPasswordChange
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
async def update_me(changes: UserUpdate, user: CurrentUser, clock: ClockDep) -> UserOut:
    updated = await UserService(UserRepository(clock), clock).update_profile(user, changes)
    return UserOut.from_user(updated)
