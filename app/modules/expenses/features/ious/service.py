"""IOUs (U2): money lent to or borrowed from someone, and what is paid back. Each payment is a
line of the loan, added once per Idempotency-Key, and what is paid back always stays between
nothing and the amount. A loan with a due date reminds the user (ADR-0012) until it is paid
back in full. Loans never touch the user's entries."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from app.modules.expenses.domain import MODULE, EntrySource
from app.modules.expenses.features.ious.models import (
    Iou,
    IouDirection,
    IouRepository,
    Settlement,
)
from app.modules.expenses.features.ious.schemas import (
    IouIn,
    IouOut,
    IouPatch,
    IouState,
    IouSummary,
    PersonBalance,
    SettlementIn,
)
from app.modules.expenses.ledger import append_once
from app.modules.expenses.planning import planning
from app.platform import (
    NEWEST_FIRST,
    ActorContext,
    AppError,
    PageParams,
    Reminders,
    normalise,
    patched,
)

NEWEST_LENT: Sequence[tuple[str, int]] = (("date", -1), *NEWEST_FIRST)
STATES: dict[IouState, dict[str, Any]] = {
    IouState.OPEN: {"settled_on": None},
    IouState.SETTLED: {"settled_on": {"$ne": None}},
    IouState.ALL: {},
}


class SettlementInvalidError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_SETTLEMENT_INVALID"
    default_message = (
        "That payment doesn't fit this loan: what is paid back stays between nothing and the "
        "amount, and is dated between the day of the loan and today."
    )


class BelowSettledError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_IOU_BELOW_SETTLED"
    default_message = "The amount can't be less than what has been paid back already."


def _settled_on(amount: int, settled: int, lines: Sequence[Settlement]) -> str | None:
    """The day of the payment that settled it in full, while it stays settled."""
    return lines[-1].date if lines and settled >= amount else None


def _refusal(iou: Iou, settled: int, day: dt.date, today: dt.date) -> str | None:
    """Why a payment can't go in, if it can't."""
    if day > today:
        return "future"
    if day < dt.date.fromisoformat(iou.date):
        return "before_loan"  # paid back before it was lent
    if settled > iou.amount_minor:
        return "too_much"  # more than is owed
    if settled < 0:
        return "below_zero"  # a correction larger than what was paid
    return None


def _due(iou: Iou | None) -> dt.date | None:
    """When to remind of the loan: its due date while it is there and open."""
    if iou is None or iou.deleted_at is not None or iou.settled_on is not None:
        return None
    return dt.date.fromisoformat(iou.due_on) if iou.due_on else None


class Ious:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repo = IouRepository(ctx.scope, ctx.clock)
        self.reminders = Reminders(ctx)

    # ------------------------------------------------------------------ reads

    async def overview(
        self, params: PageParams, state: IouState, person: str | None
    ) -> tuple[list[IouOut], int]:
        """The user's loans, most recently lent first: open, settled or all, and with one
        person (by name as compared) when asked."""
        filters = dict(STATES[state])
        if person:
            filters["counterparty_key"] = normalise(person)
        found, total = await self.repo.list(params, NEWEST_LENT, filters)
        return [IouOut.of(iou) for iou in found], total

    async def one(self, iou_id: str) -> IouOut:
        """One loan with its payments (a notification's link names the loan, not the person)."""
        return IouOut.of(await self.repo.get(iou_id))

    async def summary(self) -> IouSummary:
        """What is owed to the user and what they owe, in all and by person, in their base
        currency. A person's name is shown as most recently written."""
        currency = self.ctx.user.base_currency
        outstanding = {"$subtract": ["$amount_minor", "$settled_minor"]}
        is_open = {"$eq": ["$settled_on", None]}

        def owed(direction: IouDirection) -> dict[str, Any]:
            return {"$sum": {"$cond": [{"$eq": ["$direction", direction]}, outstanding, 0]}}

        cursor = await Iou.get_pymongo_collection().aggregate(
            [
                {"$match": self.repo.query({"currency": currency})},
                {"$sort": dict(NEWEST_LENT)},
                {
                    "$group": {
                        "_id": "$counterparty_key",
                        "name": {"$first": "$counterparty"},
                        "lent": owed(IouDirection.LENT),
                        "borrowed": owed(IouDirection.BORROWED),
                        "open": {"$sum": {"$cond": [is_open, 1, 0]}},
                        "next_due_on": {"$min": {"$cond": [is_open, "$due_on", None]}},
                    }
                },
            ]
        )
        today = self.ctx.today().isoformat()
        people = [
            PersonBalance(
                name=row["name"],
                key=row["_id"],
                owed_to_me_minor=row["lent"],
                i_owe_minor=row["borrowed"],
                net_minor=row["lent"] - row["borrowed"],
                open=row["open"],
                next_due_on=row["next_due_on"],
                overdue=row["next_due_on"] is not None and row["next_due_on"] < today,
            )
            async for row in cursor
        ]
        people.sort(key=lambda p: (p.open == 0, p.next_due_on or dt.date.max, p.name.casefold()))
        return IouSummary(
            currency=currency,
            owed_to_me_minor=sum(person.owed_to_me_minor for person in people),
            i_owe_minor=sum(person.i_owe_minor for person in people),
            people=people,
        )

    # ------------------------------------------------------------------ writes

    @staticmethod
    def _stored(body: IouIn, day: dt.date) -> dict[str, Any]:
        return {
            **body.model_dump(mode="json"),
            "date": (body.date or day).isoformat(),
            "counterparty_key": normalise(body.counterparty),
        }

    async def create(self, body: IouIn, client_key: str | None) -> tuple[IouOut, bool]:
        """The new loan, or the one an earlier request with this key made (then ``False``)."""
        fields = {**self._stored(body, self.ctx.today()), "currency": self.ctx.user.base_currency}
        iou, created = await self.repo.create_once(fields, client_key, source=EntrySource.MANUAL)
        await self._remind(str(iou.id))  # also on a replay: the first request may have stopped
        return IouOut.of(iou), created

    async def update(self, iou_id: str, body: IouPatch) -> IouOut:
        current = await self.repo.get(iou_id)
        checked = patched(IouIn, current, body)
        if checked.amount_minor < current.settled_minor:
            raise BelowSettledError(details={"settled_minor": current.settled_minor})
        changes = {
            **self._stored(checked, dt.date.fromisoformat(current.date)),
            "settled_on": _settled_on(
                checked.amount_minor, current.settled_minor, current.settlements
            ),
        }
        iou = await self.repo.update(
            iou_id, changes, expected=body.revision_no, source=EntrySource.MANUAL
        )
        await self._remind(str(iou.id))
        return IouOut.of(iou)

    async def delete(self, iou_id: str) -> None:
        iou = await self.repo.get(iou_id)
        await self.repo.soft_delete(iou_id, source=EntrySource.MANUAL)
        await self._remind(str(iou.id))  # as stored: the path may spell the id otherwise

    async def undelete(self, iou_id: str) -> IouOut:
        iou = await self.repo.undelete(iou_id, source=EntrySource.MANUAL)
        await self._remind(str(iou.id))
        return IouOut.of(iou)

    async def settle(
        self, iou_id: str, body: SettlementIn, client_key: str | None
    ) -> tuple[IouOut, bool]:
        """Record money paid back (or correct a payment with a negative amount), once per key.
        The second value is ``False`` when this key's payment was already in."""
        today = self.ctx.today()
        day = body.date or today
        line = Settlement(
            key=client_key or uuid.uuid4().hex,  # no key: nothing to replay, always added
            amount_minor=body.amount_minor,
            date=day.isoformat(),
            note=body.note,
        )

        def check(iou: Iou, item: Settlement) -> dict[str, Any]:
            settled = iou.settled_minor + item.amount_minor
            if reason := _refusal(iou, settled, day, today):
                outstanding = iou.amount_minor - iou.settled_minor
                raise SettlementInvalidError(
                    details={"reason": reason, "outstanding_minor": outstanding}
                )
            return {
                "settled_minor": settled,
                "settled_on": _settled_on(iou.amount_minor, settled, [*iou.settlements, item]),
            }

        iou, added = await append_once(
            self.repo, iou_id, "settlements", line, check=check, source=EntrySource.MANUAL
        )
        await self._remind(str(iou.id))
        return IouOut.of(iou), added

    async def _remind(self, iou_id: str) -> None:
        """Remind of an open loan's due date (planning.yaml says how many days before), else
        stop reminding. A payment saved at the same moment may settle it just after it is
        scheduled, so it is looked at again then: the last write always decides."""
        iou = await self.repo.find(iou_id, include_deleted=True)
        due_on = _due(iou)
        if iou is not None and due_on is not None:
            await self.reminders.schedule(
                f"{MODULE}.iou_due_{iou.direction}",
                module=MODULE,
                ref_id=iou_id,
                due_on=due_on,
                days_before=planning().ious.remind_days_before,
                iou_id=iou_id,
                person=iou.counterparty,
                outstanding_minor=iou.amount_minor - iou.settled_minor,
                currency=iou.currency,
            )
            iou = await self.repo.find(iou_id, include_deleted=True)
        if _due(iou) is None:
            await self.reminders.cancel(module=MODULE, ref_id=iou_id)
