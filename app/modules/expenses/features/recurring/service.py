"""Repeating entries and bills (U3). A rule adds its own entries on their due dates, or, for a
bill, reminds the user (ADR-0012) until they mark it paid: a bill stays due until then. Each due
date makes one entry at most, however many runs meet: its client key is
``recurring/<rule>/<due date>`` (a ``/`` never appears in a client's Idempotency-Key). What
repeats in the entries is found afresh each time it is asked for; only the user's "not
regular" answers are kept."""

from __future__ import annotations

import datetime as dt
from contextlib import suppress
from typing import Any, ClassVar

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.modules.expenses.domain import MODULE, EntrySource, RuleMode
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.features.entries.schemas import EntryIn
from app.modules.expenses.features.entries.service import EntryService
from app.modules.expenses.features.recurring.cadence import (
    Cadence,
    after,
    between,
    falls_on,
    on_or_after,
)
from app.modules.expenses.features.recurring.detect import Repeat, detect, group_of
from app.modules.expenses.features.recurring.models import (
    DismissedRepository,
    RecurringRule,
    RecurringSuggestion,
    RuleRepository,
    RuleSource,
)
from app.modules.expenses.features.recurring.schemas import (
    MarkPaidIn,
    RuleIn,
    RuleOut,
    RulePatch,
    SuggestionOut,
    TemplateFields,
)
from app.modules.expenses.planning import planning
from app.platform import ActorContext, AppError, PageParams, Reminders, get_logger, patched

logger = get_logger(__name__)
SOONEST_DUE = (("next_run", 1), ("_id", 1))


class RecurringNotDueError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_RECURRING_NOT_DUE"
    default_message = "That isn't a due date of this bill yet: mark the next one paid first."


def _key(rule: RecurringRule, due: dt.date) -> str:
    return f"recurring/{rule.id}/{due}"


def _entry(rule: RecurringRule, day: dt.date, **changes: Any) -> EntryIn:
    """The entry a rule adds on ``day``, in the rule's own currency: once the user's base
    currency has changed it is refused, never relabelled (the amount is in the rule's)."""
    fields = {**rule.template.model_dump(), "currency": rule.currency, "date": day, **changes}
    return EntryIn.model_validate(fields)


def _bill(rule: RecurringRule) -> bool:
    """A bill to remind of: switched on, and in remind mode."""
    return rule.active and rule.mode is RuleMode.REMIND


async def _set_next(rule: RecurringRule, start: str, following: str) -> bool:
    """Set ``next_run`` to ``following`` only if it is still ``start``: of two runs or changes
    at the same moment, one moves it, and the other learns it lost."""
    result = await RecurringRule.get_pymongo_collection().update_one(
        {"_id": rule.id, "next_run": start}, {"$set": {"next_run": following}}
    )
    return bool(result.modified_count)


def _next_on(repeat: Repeat, today: dt.date) -> dt.date:
    """When something that repeats is next due: after it was last seen, and not before today."""
    seen = Cadence(unit=repeat.unit, every=1, anchor=repeat.latest.date)
    return after(seen, max(repeat.last_on, today - dt.timedelta(days=1)))


class Recurring:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repo = RuleRepository(ctx.scope, ctx.clock)
        self.dismissed = DismissedRepository(ctx.scope, ctx.clock)
        self.entries = EntryService(ctx)
        self.categories = Categories(ctx.scope, ctx.clock)
        self.reminders = Reminders(ctx)

    # ------------------------------------------------------------------ reads

    async def overview(self, params: PageParams) -> tuple[list[RuleOut], int]:
        """The user's rules, soonest due first, so unpaid bills lead."""
        rules, total = await self.repo.list(params, SOONEST_DUE)
        today = self.ctx.today()
        return [RuleOut.of(rule, today) for rule in rules], total

    async def suggestions(self) -> list[SuggestionOut]:
        """What repeats in the entries the user made (not those a rule made), unless a rule is
        already about the same thing or the user said it isn't regular."""
        today, tuning = self.ctx.today(), planning().recurring.detect
        since = today - dt.timedelta(days=tuning.lookback_days)
        own = {
            "recurring_id": None,
            "currency": self.ctx.user.base_currency,
            "date": {"$gte": since.isoformat()},
        }
        seen = await Entry.find(self.entries.entries.query(own)).sort("date").to_list()
        rules = await RecurringRule.find(self.repo.query()).to_list()
        taken = {group_of(rule.template.kind, rule.template.title) for rule in rules}
        dismissed = set(
            await RecurringSuggestion.get_pymongo_collection().distinct(
                "signature", self.dismissed.query()
            )
        )
        return [
            SuggestionOut.of(repeat, _next_on(repeat, today))
            for repeat in detect(seen, today, tuning)
            if repeat.group not in taken and repeat.signature not in dismissed
        ]

    # ------------------------------------------------------------------ writes

    async def _usable(self, template: TemplateFields) -> None:
        await self.categories.ensure_usable(template.category_id, template.kind)

    async def _settled(self, rule: RecurringRule) -> RuleOut:
        """After a rule is saved: add what is due today, and remind of a bill (or stop)."""
        await self.materialize(rule.id)
        await self._remind(str(rule.id))
        return RuleOut.of(await self.repo.get(str(rule.id)), self.ctx.today())

    async def create(self, body: RuleIn) -> tuple[RuleOut, bool]:
        """The new rule, or, for a suggestion confirmed again, the rule already made from it
        (then ``False``). It starts at its first due date from today, never before."""
        await self._usable(body.template)
        fields = body.model_dump(mode="json")
        first = on_or_after(Cadence.model_validate(fields["cadence"]), self.ctx.today())
        fields |= {
            "next_run": first.isoformat(),
            "currency": self.ctx.user.base_currency,
            "source": RuleSource.DETECTED if body.signature else RuleSource.MANUAL,
        }
        try:
            rule = await self.repo.create(fields)
        except DuplicateKeyError:
            earlier = await RecurringRule.find_one(self.repo.query({"signature": body.signature}))
            if earlier is None:
                raise
            return RuleOut.of(earlier, self.ctx.today()), False
        return await self._settled(rule), True

    async def update(self, rule_id: str, body: RulePatch) -> RuleOut:
        current = await self.repo.get(rule_id)
        checked = patched(RuleIn, current, body)
        was = current.template
        # A rule keeps the category it was made with, even archived (as entries do), and files
        # what it adds there too.
        if (checked.template.category_id, checked.template.kind) != (was.category_id, was.kind):
            await self._usable(checked.template)
        # next_run is not written back: a bill paid at the same moment stays paid.
        rule = await self.repo.update(
            rule_id, checked.model_dump(mode="json", exclude={"signature"})
        )
        # A new pace starts afresh from today. A rule switched back on, or that now adds its
        # own entries, goes on from today too, or from a later due date already paid.
        new_pace = rule.cadence != current.cadence
        adding = checked.mode is RuleMode.AUTO_CREATE and current.mode is not RuleMode.AUTO_CREATE
        if new_pace or (checked.active and (adding or not current.active)):
            await self._start_again(rule, keep=not new_pace)
        return await self._settled(rule)

    async def delete(self, rule_id: str) -> None:
        """Its entries stay. A rule confirmed from a suggestion is not suggested again."""
        rule = await self.repo.get(rule_id)
        stored_id = str(rule.id)  # as stored: the path may spell it otherwise
        if rule.signature:
            await self.dismiss(rule.signature)
            await self.repo.update(stored_id, {"signature": None})  # free to confirm again
        await self.repo.soft_delete(stored_id)
        await self.reminders.cancel(module=MODULE, ref_id=stored_id)

    async def dismiss(self, signature: str) -> None:
        """Not regular: it is never suggested again. Saying so twice changes nothing."""
        with suppress(DuplicateKeyError):
            await self.dismissed.create(
                {"signature": signature, "dismissed_at": self.ctx.clock.now()}
            )

    async def mark_paid(self, rule_id: str, body: MarkPaidIn) -> tuple[Entry, bool]:
        """Add the entry of a due date paid, once per due date (``False``: it was in already).
        Paying the next due date moves the bill on to the one after, and its reminder too."""
        rule = await self.repo.get(rule_id)
        due = body.due_on
        if due.isoformat() > rule.next_run or not falls_on(rule.cadence, due):
            raise RecurringNotDueError(details={"next_run": rule.next_run})
        amount = {"amount_minor": body.amount_minor} if body.amount_minor else {}
        entry, created = await self.entries.create(
            _entry(rule, body.date or self.ctx.today(), **amount),
            _key(rule, due),
            recurring_id=str(rule.id),
        )
        if due.isoformat() == rule.next_run:
            await self._move(rule, rule.next_run, due)
        await self._remind(str(rule.id))
        return entry, created

    # ------------------------------------------------------------------ due dates

    async def materialize(self, only: PydanticObjectId | None = None) -> int:
        """Add the entries rules that add their own have come due for, up to today (``only``:
        that rule's). Returns how many were added."""
        today = self.ctx.today()
        due = {"mode": RuleMode.AUTO_CREATE, "active": True, "next_run": {"$lte": str(today)}}
        if only is not None:
            due["_id"] = only
        made = 0
        for rule in await RecurringRule.find(self.repo.query(due)).to_list():
            try:
                made += await self._catch_up(rule, today)
            except AppError as error:  # e.g. not in the base currency now: the others go on
                logger.warning("recurring_rule_failed", rule_id=str(rule.id), code=error.code)
        return made

    async def _catch_up(self, rule: RecurringRule, today: dt.date) -> int:
        """Each due date from ``next_run`` to today adds its entry once (by its client key), at
        most ``recurring.max_catch_up`` of them (the latest), and moves ``next_run`` past it,
        so a run cut short keeps what it did."""
        first = dt.date.fromisoformat(rule.next_run)
        dues = between(rule.cadence, first, today)[-planning().recurring.max_catch_up :]
        made, start = 0, rule.next_run
        for due in dues:
            _, created = await self.entries.create(
                _entry(rule, due), _key(rule, due), EntrySource.RECURRING, recurring_id=str(rule.id)
            )
            made += created
            moved = await self._move(rule, start, due)
            if moved is None:
                break  # another run has moved on, and adds the rest
            start = moved
        return made

    async def _move(self, rule: RecurringRule, start: str, done: dt.date) -> str | None:
        """Move ``next_run`` from ``start`` to the due date after ``done``. Returns it, or
        ``None`` when another run or a change has moved the rule already."""
        following = after(rule.cadence, done).isoformat()
        return following if await _set_next(rule, start, following) else None

    async def _start_again(self, rule: RecurringRule, *, keep: bool) -> None:
        """Move ``next_run`` to the first due date from today, so nothing before today is added
        (``keep``: unless its own is later, e.g. a bill paid ahead). A due date paid or added at
        the same moment is read again, never undone."""
        while True:
            first = on_or_after(rule.cadence, self.ctx.today()).isoformat()
            target = max(first, rule.next_run) if keep else first
            if target == rule.next_run or await _set_next(rule, rule.next_run, target):
                return
            rule = await self.repo.get(str(rule.id))

    async def _remind(self, rule_id: str) -> None:
        """Remind of a bill's next due date (its own days before), else stop reminding. A bill
        paid or changed at the same moment may move on while this schedules, so it is read
        again until what was scheduled is what the rule says: the last write decides."""
        scheduled: str | None = None
        rule = await self.repo.find(rule_id)
        while rule is not None and _bill(rule) and rule.next_run != scheduled:
            scheduled = rule.next_run
            await self.reminders.schedule(
                f"{MODULE}.bill_due",
                module=MODULE,
                ref_id=rule_id,
                due_on=dt.date.fromisoformat(scheduled),
                days_before=rule.remind_days_before,
                rule_id=rule_id,
                title=rule.template.title,
                amount_minor=rule.template.amount_minor,
                currency=rule.currency,
            )
            rule = await self.repo.find(rule_id)
        if rule is None or not _bill(rule):
            await self.reminders.cancel(module=MODULE, ref_id=rule_id)
