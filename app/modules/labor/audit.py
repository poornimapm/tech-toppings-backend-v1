"""The audit trail: every change the services make, with who made it, when (the server's
clock), to which project and record, and each field from and to.

An entry is written after its change has been saved, so a change that failed leaves none; no
route edits or removes one. Nothing secret is ever in one: a file is recorded as changed, never
its contents."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from beanie import PydanticObjectId

from app.modules.labor.models import AuditRepository, Change
from app.platform import ActorContext, current_request_id


def changes(
    before: Mapping[str, Any], after: Mapping[str, Any], fields: Iterable[str]
) -> list[Change]:
    """Each of ``fields`` whose value differs, from and to."""
    return [
        Change(field=name, before=before.get(name), after=after.get(name))
        for name in fields
        if before.get(name) != after.get(name)
    ]


class Audit:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repository = AuditRepository(ctx.scope, ctx.clock)

    async def record(
        self,
        entity: str,
        entity_id: str,
        action: str,
        *,
        project_id: PydanticObjectId | None = None,
        changed: list[Change] | None = None,
        note: str | None = None,
    ) -> None:
        user = self.ctx.user
        await self.repository.create(
            {
                "at": self.ctx.clock.now(),
                "actor_id": user.id,
                "actor_name": user.name,
                "actor_role": str(user.role),
                "project_id": project_id,
                "entity": entity,
                "entity_id": entity_id,
                "action": action,
                "changes": [change.model_dump() for change in changed or []],
                "note": note,
                "request_id": current_request_id(),
            }
        )
