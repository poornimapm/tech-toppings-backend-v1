"""Goals (U1): something to save for, with a target and maybe a date. A manual goal grows by the
money the user adds by hand (each contribution once per Idempotency-Key); a net-savings goal
grows with what they keep, money in less money out, from a day they choose. Reaching a goal is
told once for each target."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any, ClassVar

from app.modules.expenses.domain import MODULE, EntrySource
from app.modules.expenses.features.analytics.schemas import Span
from app.modules.expenses.features.analytics.service import Analytics
from app.modules.expenses.features.goals.models import (
    Contribution,
    Goal,
    GoalFunding,
    GoalRepository,
)
from app.modules.expenses.features.goals.schemas import (
    ContributionIn,
    GoalIn,
    GoalOut,
    GoalPatch,
    Progress,
)
from app.modules.expenses.ledger import append_once
from app.platform import NEWEST_FIRST, ActorContext, AppError, Inbox, PageParams, patched

PERCENT = 100
MONTHS = 12


class ContributionInvalidError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_CONTRIBUTION_INVALID"
    default_message = (
        "That can't go into this goal: money is added by hand only to goals funded by hand, "
        "dated today or earlier, and no more can be taken out than is saved."
    )


def _months(today: dt.date, until: dt.date) -> int:
    """Calendar months from this one to ``until``'s, both counted."""
    return (until.year - today.year) * MONTHS + until.month - today.month + 1


def progress(goal: Goal, saved: int, today: dt.date) -> Progress:
    """How far ``saved`` brings the goal on ``today``. What is left a month is rounded up, so
    saving it each month does reach the target in time."""
    left = max(goal.target_minor - saved, 0)
    until = dt.date.fromisoformat(goal.target_date) if goal.target_date else None
    months = _months(today, until) if until and until >= today else 0
    return Progress(
        saved_minor=saved,
        left_minor=left,
        pct=saved * PERCENT // goal.target_minor,
        per_month_minor=-(-left // months) if left and months else None,
        reached=left == 0,
    )


def _refusal(goal: Goal, saved: int, day: dt.date, today: dt.date) -> str | None:
    """Why a contribution can't go in, if it can't."""
    if goal.funding is not GoalFunding.MANUAL:
        return "net_savings"  # it grows with the entries, never by hand
    if day > today:
        return "future"
    if saved < 0:
        return "below_zero"
    return None


class Goals:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repo = GoalRepository(ctx.scope, ctx.clock)
        self.analytics = Analytics(ctx)
        self.inbox = Inbox(ctx.scope, ctx.clock, ctx.settings)

    # ------------------------------------------------------------------ reads

    async def saved(self, goal: Goal) -> int:
        """A manual goal's contributions; a net-savings goal's money in less money out from
        its first day to today (never below nothing), in the goal's own currency."""
        if goal.funding is GoalFunding.MANUAL:
            return goal.saved_minor
        span = Span(start=dt.date.fromisoformat(goal.since), end=self.ctx.today())
        return max((await self.analytics.totals(span, currency=goal.currency)).net_minor, 0)

    async def shown(self, goal: Goal) -> GoalOut:
        """The goal with how far it has come. Reaching it is told here, so a net-savings goal,
        which grows with every entry, is noticed whenever it is looked at."""
        found = progress(goal, await self.saved(goal), self.ctx.today())
        if found.reached:
            await self._reached(goal)
        return GoalOut.of(goal, found)

    async def overview(self, params: PageParams) -> tuple[list[GoalOut], int]:
        """The user's goals, newest first, each with how far it has come."""
        goals, total = await self.repo.list(params, NEWEST_FIRST)
        return list(await asyncio.gather(*(self.shown(goal) for goal in goals))), total

    # ------------------------------------------------------------------ writes

    def _stored(self, body: GoalIn, since: dt.date) -> dict[str, Any]:
        return {**body.model_dump(mode="json"), "since": (body.since or since).isoformat()}

    async def create(self, body: GoalIn, client_key: str | None) -> tuple[GoalOut, bool]:
        """The new goal, or the one an earlier request with this key made (then ``False``)."""
        fields = {**self._stored(body, self.ctx.today()), "currency": self.ctx.user.base_currency}
        goal, created = await self.repo.create_once(fields, client_key, source=EntrySource.MANUAL)
        return await self.shown(goal), created

    async def update(self, goal_id: str, body: GoalPatch) -> GoalOut:
        current = await self.repo.get(goal_id)
        checked = patched(GoalIn, current, body)
        goal = await self.repo.update(
            goal_id,
            self._stored(checked, dt.date.fromisoformat(current.since)),
            expected=body.revision_no,
            source=EntrySource.MANUAL,
        )
        return await self.shown(goal)

    async def delete(self, goal_id: str) -> None:
        await self.repo.soft_delete(goal_id, source=EntrySource.MANUAL)

    async def undelete(self, goal_id: str) -> GoalOut:
        return await self.shown(await self.repo.undelete(goal_id, source=EntrySource.MANUAL))

    async def contribute(
        self, goal_id: str, body: ContributionIn, client_key: str | None
    ) -> tuple[GoalOut, bool]:
        """Add money to a manual goal, or take some out (a negative amount), once per key. The
        second value is ``False`` when this key's contribution was already in."""
        today = self.ctx.today()
        day = body.date or today
        item = Contribution(
            key=client_key or uuid.uuid4().hex,  # no key: nothing to replay, always added
            amount_minor=body.amount_minor,
            date=day.isoformat(),
            note=body.note,
        )

        def check(goal: Goal, line: Contribution) -> dict[str, int]:
            saved = goal.saved_minor + line.amount_minor
            if reason := _refusal(goal, saved, day, today):
                # What was put in by hand: a net-savings goal has none to tell.
                known = {} if reason == "net_savings" else {"saved_minor": goal.saved_minor}
                raise ContributionInvalidError(details={"reason": reason, **known})
            return {"saved_minor": saved}

        goal, added = await append_once(
            self.repo, goal_id, "contributions", item, check=check, source=EntrySource.MANUAL
        )
        return await self.shown(goal), added

    async def _reached(self, goal: Goal) -> None:
        """Tell the user once for each target: raising the target and reaching it again is told
        again, taking money out and putting it back is not."""
        target = goal.target_minor
        if goal.told_reached_minor == target:
            return
        await self.inbox.notify(
            f"{MODULE}.goal_reached",
            module=MODULE,
            dedupe_key=f"{MODULE}.goal_reached:{goal.id}:{target}",
            goal_id=str(goal.id),
            name=goal.name,
            target_minor=target,
            currency=goal.currency,
        )
        # Told first, then marked, as budgets do: looks at the same moment are deduped by the
        # notification, and the mark keeps it told after the notification is purged.
        await Goal.get_pymongo_collection().update_one(
            {"_id": goal.id, "told_reached_minor": {"$ne": target}},
            {"$set": {"told_reached_minor": target}},
        )
