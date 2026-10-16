"""``/v1/notifications``: my notifications. The unread count is the ``total`` of
``?unread=true`` (ask for ``page_size=1``), so the bell needs no endpoint of its own. The
bell's poll is also when due jobs run for the user (ADR-0007), so what they deliver is listed
in the same response."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.core.errors import error_responses
from app.core.pagination import Page, PageParamsDep
from app.core.repository import NEWEST_FIRST
from app.platform.context import ActorDep
from app.platform.jobs.deps import CaughtUpDep
from app.platform.notifications.inbox import Inbox, Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


def inbox_of(ctx: ActorDep) -> Inbox:
    return Inbox(ctx.scope, ctx.clock, ctx.settings)


InboxDep = Annotated[Inbox, Depends(inbox_of)]


class NotificationOut(BaseModel):
    id: str
    type: str
    params: dict[str, Any]
    read: bool
    at: datetime

    @classmethod
    def of(cls, item: Notification) -> NotificationOut:
        return cls(
            id=str(item.id),
            type=item.type,
            params=item.params,
            read=item.read_at is not None,
            at=item.created_at,
        )


@router.get("", summary="My notifications, newest first", responses=error_responses(401))
async def list_notifications(
    _: CaughtUpDep,
    inbox: InboxDep,
    params: PageParamsDep,
    unread: Annotated[bool | None, Query(description="true = only unread")] = None,
) -> Page[NotificationOut]:
    filters = {"read_at": None} if unread else {}
    items, total = await inbox.list(params, NEWEST_FIRST, filters)
    return Page[NotificationOut].build([NotificationOut.of(item) for item in items], params, total)


@router.post(
    "/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark everything as read",
    responses=error_responses(401),
)
async def read_all(inbox: InboxDep) -> None:
    await inbox.mark_all_read()


@router.post(
    "/{notification_id}/read", summary="Mark one as read", responses=error_responses(401, 404)
)
async def read_one(notification_id: str, inbox: InboxDep) -> NotificationOut:
    return NotificationOut.of(await inbox.mark_read(notification_id))


@router.delete(
    "/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove one",
    responses=error_responses(401, 404),
)
async def remove_one(notification_id: str, inbox: InboxDep) -> None:
    await inbox.soft_delete(notification_id)
