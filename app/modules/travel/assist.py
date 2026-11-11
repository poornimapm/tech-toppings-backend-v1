"""Asking for AI help: each request starts one skill of the ``travel`` agent in the background
(``start_skill``) and answers at once with the run, which the page follows at
``GET /v1/agents/runs/{id}``. Everything is checked before a run starts (the trip is the person's
own and in Basic, the day is in it, the activity is of it and still planned), so a refusal costs
no AI call. The run's context carries ids only: the model reads the trip through its tools, which
leave out what it must never see."""

from __future__ import annotations

from typing import Any

from app.modules.travel.ai import ensure_ai
from app.modules.travel.drafts import activity_on
from app.modules.travel.events import ensure_in_trip
from app.modules.travel.schemas import (
    AssistTask,
    TravelAssistIn,
    TravelAssistOut,
    TravelDescribeIn,
)
from app.modules.travel.trips import Trips
from app.platform import ActorContext, AgentRun, start_skill

DESCRIBE = "describe-trip"
SKILLS = {
    AssistTask.DAY: "plan-day",
    AssistTask.ACTIVITY: "activity-checklist",
    AssistTask.CHANGE: "update-trip",
    AssistTask.ASK: "ask-trip",
}


def _started(run: AgentRun) -> TravelAssistOut:
    return TravelAssistOut(run_id=str(run.id))


class Assist:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)

    def _locale(self, asked: str | None) -> str:
        return asked or self.ctx.user.locale

    async def describe(self, body: TravelDescribeIn) -> TravelAssistOut:
        """A trip not made yet: its draft fills the planning steps."""
        run = await start_skill(self.ctx, DESCRIBE, body.text, locale=self._locale(body.locale))
        return _started(run)

    async def on_trip(self, trip_id: str, body: TravelAssistIn) -> TravelAssistOut:
        """The person's words, or else what the task is about (the day, the activity's title), are
        the run's input: data the skill reads, never instructions written here."""
        trip = ensure_ai(await self.trips.get(trip_id))
        context: dict[str, Any] = {"trip_id": str(trip.id)}
        subject = ""
        if body.task is AssistTask.DAY and body.day:
            subject = context["day"] = body.day.isoformat()
            ensure_in_trip(trip, subject)
        if body.task in (AssistTask.ACTIVITY, AssistTask.CHANGE) and body.activity_id:
            activity = await activity_on(self.ctx, trip, body.activity_id)
            context |= {"activity_id": str(activity.id), "day": activity.day}
            subject = activity.title
        text = body.text or subject
        locale = self._locale(body.locale)
        run = await start_skill(self.ctx, SKILLS[body.task], text, locale=locale, context=context)
        return _started(run)
