"""``/v1/reminders``: what I'll be reminded of, with snooze and dismiss. Listing first runs the
jobs that are due (ADR-0007), so a reminder that has just come due is already told."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import error_responses
from app.core.pagination import Page, PageParamsDep
from app.platform.activity.log import with_module
from app.platform.context import ActorDep
from app.platform.jobs.deps import CaughtUpDep
from app.platform.reminders.models import Reminder
from app.platform.reminders.service import Reminders, notice_params

router = APIRouter(prefix="/reminders", tags=["reminders"])


def reminders_of(ctx: ActorDep) -> Reminders:
    return Reminders(ctx)


RemindersDep = Annotated[Reminders, Depends(reminders_of)]


class ReminderOut(BaseModel):
    id: str
    type: str
    params: dict[str, Any] = Field(description="As its notification carries them")
    due_on: date
    remind_at: datetime = Field(description="When it is (or was) told")
    sent: bool
    snoozed_until: datetime | None

    @classmethod
    def of(cls, item: Reminder) -> ReminderOut:
        return cls(
            id=str(item.id),
            type=item.type,
            params=with_module(item.module_key, notice_params(item)),
            due_on=date.fromisoformat(item.due_on),
            remind_at=item.remind_at,
            sent=item.sent_at is not None,
            snoozed_until=item.snoozed_until,
        )


class SnoozeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(ge=1, description="Days from today; at most REMINDERS_MAX_SNOOZE_DAYS")


@router.get("", summary="My open reminders, soonest due first", responses=error_responses(401))
async def list_reminders(
    _: CaughtUpDep, reminders: RemindersDep, params: PageParamsDep
) -> Page[ReminderOut]:
    items, total = await reminders.open(params)
    return Page[ReminderOut].build([ReminderOut.of(item) for item in items], params, total)


@router.post(
    "/{reminder_id}/snooze",
    summary="Remind me again in a few days",
    responses=error_responses(401, 404),
)
async def snooze(reminder_id: str, body: SnoozeIn, reminders: RemindersDep) -> ReminderOut:
    return ReminderOut.of(await reminders.snooze(reminder_id, body.days))


@router.post(
    "/{reminder_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop reminding me",
    responses=error_responses(401, 404),
)
async def dismiss(reminder_id: str, reminders: RemindersDep) -> None:
    await reminders.dismiss(reminder_id)
