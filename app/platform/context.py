"""Who is acting: the one object handed to every plugin function (agent tools, dashboard
widgets ...), so they all see the caller the same way and stay inside the caller's Scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from app.core.clock import Clock
from app.core.config import Settings
from app.core.deps import ClockDep, SettingsDep
from app.core.repository import Scope
from app.platform.auth.deps import CurrentUser, UserScope
from app.platform.users.models import User


@dataclass(frozen=True)
class ActorContext:
    user: User
    scope: Scope  # the user's own data, or unrestricted on /v1/admin routes
    clock: Clock
    settings: Settings
    state: Any  # app.state: shared services (module registry, agent runtime ...)


def actor_context(
    request: Request, user: CurrentUser, scope: UserScope, clock: ClockDep, settings: SettingsDep
) -> ActorContext:
    return ActorContext(
        user=user, scope=scope, clock=clock, settings=settings, state=request.app.state
    )


ActorDep = Annotated[ActorContext, Depends(actor_context)]
