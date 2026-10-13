"""Agent tools for the user's notifications."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from app.core.pagination import PageParams
from app.core.repository import NEWEST_FIRST
from app.platform.agents.tools import tool
from app.platform.context import ActorContext
from app.platform.notifications.router import NotificationOut, inbox_of


@tool("read")
async def notifications_list(
    ctx: ActorContext,
    unread_only: Annotated[bool, Field(description="false also returns ones already read")] = True,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    """The user's notifications, newest first: type (a code such as module.launched), params,
    whether it was read, and when it arrived."""
    filters = {"read_at": None} if unread_only else {}
    items, _ = await inbox_of(ctx).list(PageParams(1, limit), NEWEST_FIRST, filters)
    return [NotificationOut.of(item).model_dump(exclude={"id"}) for item in items]


@tool("write")
async def notifications_mark_all_read(ctx: ActorContext) -> dict[str, int]:
    """Mark every notification as read."""
    return {"marked": await inbox_of(ctx).mark_all_read()}


@tool("client")
async def notifications_open(ctx: ActorContext) -> None:
    """Show the user their notifications page."""
