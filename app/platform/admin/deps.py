"""What every ``/v1/admin`` route works with: the admin acting across all users, and the audit
trail their changes go into."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from app.core.deps import ClockDep, SettingsDep
from app.core.errors import UnauthenticatedError
from app.core.rate_limit import client_ip
from app.core.repository import Scope
from app.platform.audit.service import AuditLog
from app.platform.auth.deps import AdminUser
from app.platform.context import ActorContext


def admin_context(
    request: Request, admin: AdminUser, clock: ClockDep, settings: SettingsDep
) -> ActorContext:
    """The admin acting across all users: plugin code sees an unrestricted scope."""
    if admin.id is None:  # pragma: no cover - persisted users always have an id
        raise UnauthenticatedError
    return ActorContext(
        user=admin,
        scope=Scope.unrestricted(admin.id),
        clock=clock,
        settings=settings,
        state=request.app.state,
    )


AdminDep = Annotated[ActorContext, Depends(admin_context)]


@dataclass(frozen=True)
class AdminAction:
    """An admin about to change something, and the trail it goes into."""

    ctx: ActorContext
    ip: str
    audit: AuditLog

    async def record(self, action: str, *, entity: str, **facts: Any) -> None:
        await self.audit.record(
            action, actor_id=self.ctx.user.id, entity=entity, ip=self.ip, **facts
        )


def admin_action(request: Request, ctx: AdminDep) -> AdminAction:
    return AdminAction(ctx, client_ip(request), AuditLog(ctx.clock, ctx.settings))


AdminActionDep = Annotated[AdminAction, Depends(admin_action)]
