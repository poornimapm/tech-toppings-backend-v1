"""``Reminders(ctx)``: how a module reminds the user of a due date, and how due reminders
reach the inbox, and the user's devices as a push where they agreed to that (best effort).

Each due date is told exactly once, and that never rests on a lock or on the schedule:

* there is one row per (module, record, due date), and a row already sent or dismissed is
  never re-armed;
* a round is first told with the inbox's ``dedupe_key`` ``reminder:{id}:{round}`` and only then
  marked sent by a guarded update, so a crash in between just repeats a notify that is deduped.

A snooze starts the next round, which is told once more. Pushing a told round is a job of its
own (``push_due``): best effort, up to ``PUSH_MAX_TRIES`` tries, under the same tag, so a
repeat replaces it on the device.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId, SortDirection
from fastapi import status
from pymongo.errors import DuplicateKeyError

from app.core.daterange import at_local
from app.core.documents import OwnedDocument
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams
from app.core.repository import ScopedRepository
from app.platform.context import ActorContext
from app.platform.modules.service import ModuleService
from app.platform.notifications.inbox import Inbox
from app.platform.push.service import Push
from app.platform.reminders.models import Reminder

logger = get_logger(__name__)
# What a reminder adds to its notification's params itself, so a module may not use them.
RESERVED_PARAMS = frozenset({"module", "dedupe_key", "reminder_id", "ref_id", "due_on"})
SOONEST_DUE: tuple[tuple[str, int], ...] = (("due_on", 1), ("remind_at", 1), ("_id", 1))
# The last zone to start a day (UTC-12): once a day has begun there, it has begun everywhere.
LAST_ZONE = timezone(timedelta(hours=-12))


class SnoozeInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = ErrorCode.VALIDATION_ERROR
    default_message = "A snooze is a whole number of days, up to the limit."


def notice_params(item: Reminder) -> dict[str, Any]:
    """What its notification says besides the module: which reminder and due date, plus the
    module's own params."""
    return {"reminder_id": str(item.id), "due_on": item.due_on, **item.params}


def round_tag(item: Reminder) -> str:
    """The round's ``dedupe_key`` in the inbox, and its push's tag on the device."""
    return f"reminder:{item.id}:{item.round}"


def push_message(item: Reminder) -> dict[str, Any]:
    """What a push carries: the notification's type and params, and the round's tag."""
    params = {"module": item.module_key, **notice_params(item)}
    return {"type": item.type, "params": params, "tag": round_tag(item)}


class Reminders(ScopedRepository[Reminder]):
    """One user's reminders. A module schedules one per record and due date:
    ``Reminders(ctx).schedule("expenses.bill_due", module="expenses", ref_id=rule_id,
    due_on=day, days_before=2, title=title)``, and cancels it when the record is settled."""

    model: ClassVar[type[OwnedDocument]] = Reminder

    def __init__(self, ctx: ActorContext) -> None:
        super().__init__(ctx.scope, ctx.clock)
        self.ctx = ctx
        self.settings = ctx.settings.reminders

    def module_key_for(self, fields: Mapping[str, Any]) -> str:
        return str(fields["module_key"])

    # ------------------------------------------------------------------ for modules

    async def schedule(
        self,
        type_: str,
        *,
        module: str,
        ref_id: str,
        due_on: date,
        days_before: int = 0,
        **params: Any,
    ) -> None:
        """Remind the user ``days_before`` days ahead of ``due_on``, at the send hour.

        A record has one live reminder, so a new due date replaces the previous one. Scheduling
        a due date again refreshes its wording and, while it is unsent and not snoozed, its
        time; one already sent or dismissed is never told again."""
        reserved = sorted(RESERVED_PARAMS & params.keys())
        if reserved:
            raise ValueError(f"reminder params may not use {reserved}")
        if due_on + timedelta(days=self.settings.retention_days) <= self.ctx.today():
            return  # its row may be purged already: scheduling it again would tell it twice
        ref, day = {"module_key": module, "ref_id": ref_id}, due_on.isoformat()
        await self._cancel({**ref, "due_on": {"$ne": day}})
        remind_at = self._at(due_on - timedelta(days=days_before))
        timing = {"remind_at": remind_at, "purge_at": self._purge_at(day, remind_at)}
        wording = {"type": type_, "params": params}
        current = await Reminder.find_one(self.query({**ref, "due_on": day}, include_deleted=True))
        if current is None:
            with suppress(DuplicateKeyError):  # scheduled at the same moment by another run
                await self.create({**ref, **wording, **timing, "due_on": day})
        elif current.dismissed_at is None:
            keep = current.sent_at is not None or current.snoozed_until is not None
            changes = {**wording, **({} if keep else timing), "deleted_at": None}
            await Reminder.get_pymongo_collection().update_one(
                {"_id": current.id, "round": current.round, "dismissed_at": None},
                {"$set": {**changes, **self._stamp()}},
            )
        else:
            return  # dismissed: it stays that way
        await self._supersede(ref, day)

    async def cancel(self, *, module: str, ref_id: str) -> int:
        """Stop reminding of a record (paid, settled, deleted); it also leaves the user's list.
        Returns how many were live."""
        return await self._cancel({"module_key": module, "ref_id": ref_id})

    # ------------------------------------------------------------------ delivery

    async def deliver_due(self) -> int:
        """Tell what has come due, earliest first, up to ``REMINDERS_DELIVER_BATCH`` (the rest
        go next time). A module switched off for the user keeps its reminders until it is
        back. Returns how many were told."""
        now = self.clock.now()
        criteria = self.query({"sent_at": None, "dismissed_at": None, "remind_at": {"$lte": now}})
        if await Reminder.find_one(criteria) is None:
            return 0  # the usual case on every poll: one indexed read
        due = (
            await Reminder.find({**criteria, **await self._usable()})
            .sort([("remind_at", SortDirection.ASCENDING), ("_id", SortDirection.ASCENDING)])
            .limit(self.settings.deliver_batch)
            .to_list()
        )
        inbox = Inbox(self.scope, self.clock, self.ctx.settings)
        # A push is owed only where there is a browser to push to; push_due() sends it.
        push_at = self.clock.now() if await Push(self.ctx).has_browsers() else None
        told = 0
        for item in due:
            params = notice_params(item)
            await inbox.notify(
                item.type, module=item.module_key, dedupe_key=round_tag(item), **params
            )
            told += await self._mark_sent(item, push_at)
        return told

    async def _mark_sent(self, item: Reminder, push_at: datetime | None) -> int:
        """Only the round just told: a snooze in the meantime has started the next one. Only the
        run that marks it sent makes its push owed, with tries of its own."""
        owed = {"push_at": push_at, "push_claim": None, "push_tries": 0}
        result = await Reminder.get_pymongo_collection().update_one(
            {"_id": item.id, "sent_at": None, "round": item.round},
            {"$set": {"sent_at": self.clock.now(), **owed, **self._stamp()}},
        )
        return result.modified_count

    async def push_due(self) -> int:
        """Push the told rounds whose push is owed to the user's browsers, in one round of at most
        ``PUSH_MAX_PARALLEL`` pushes at a time (ADR-0018).
        A job of its own, so a slow push service never holds up the inbox. What is owed is
        claimed for ``JOBS_LEASE_SECONDS`` first, each claim a try. A push tried on every
        browser is done, even in a round cut short (``PUSH_TIMEOUT_SECONDS``); the rest, and
        all of a run killed (the job's time limit, a restart), go again once the claim passes,
        up to ``PUSH_MAX_TRIES`` tries. Returns how many were pushed."""
        claimed, claim = await self._claim_pushes()
        if not claimed:
            return 0
        sent = await Push(self.ctx).send(*(push_message(item) for item in claimed))
        tries = self.ctx.settings.push.max_tries
        pushed = [item.id for item, done in zip(claimed, sent.done, strict=True) if done]
        given_up = [
            item.id
            for item, done in zip(claimed, sent.done, strict=True)
            if not done and item.push_tries >= tries
        ]
        if given_up:
            logger.warning("reminder_push_given_up", reminders=len(given_up), tries=tries)
        await Reminder.get_pymongo_collection().update_many(
            {"_id": {"$in": pushed + given_up}, "push_claim": claim},
            {"$set": {"push_at": None}},
        )
        return len(pushed)

    async def _claim_pushes(self) -> tuple[list[Reminder], PydanticObjectId]:
        """This run takes what is owed, earliest first, in one write under a claim of its own;
        one running at the same moment (another tab, the cron) then finds it taken. A told
        round that has had its tries is owed no more. Not stamped as a change: ``updated_at``
        orders the user's writes."""
        now, claim = self.clock.now(), PydanticObjectId()
        owed = self.query(
            {
                "push_at": {"$lte": now},
                "sent_at": {"$ne": None},
                "dismissed_at": None,
                "push_tries": {"$not": {"$gte": self.ctx.settings.push.max_tries}},
            }
        )
        items = (
            await Reminder.find(owed)
            .sort([("push_at", SortDirection.ASCENDING), ("_id", SortDirection.ASCENDING)])
            .limit(self.settings.deliver_batch)
            .to_list()
        )
        if not items:
            return [], claim  # the usual case on every poll: one indexed read
        ids = [item.id for item in items]
        until = now + timedelta(seconds=self.ctx.settings.jobs.lease_seconds)
        await Reminder.get_pymongo_collection().update_many(
            {**owed, "_id": {"$in": ids}},
            {"$set": {"push_at": until, "push_claim": claim}, "$inc": {"push_tries": 1}},
        )
        return await Reminder.find({"_id": {"$in": ids}, "push_claim": claim}).to_list(), claim

    # ------------------------------------------------------------------ for the user

    async def open(self, params: PageParams) -> tuple[list[Reminder], int]:
        """Reminders not dismissed or cancelled, of modules the user has on; soonest due first."""
        return await self.list(params, SOONEST_DUE, {"dismissed_at": None, **await self._usable()})

    async def snooze(self, reminder_id: str, days: int) -> Reminder:
        """Remind again ``days`` days from today, at the send hour: once more, as a new round."""
        if not 1 <= days <= self.settings.max_snooze_days:
            raise SnoozeInvalidError(details={"max_days": self.settings.max_snooze_days})
        current = await self.get(reminder_id)
        remind_at = self._at(self.ctx.today() + timedelta(days=days))
        changes = {
            "remind_at": remind_at,
            "snoozed_until": remind_at,
            "sent_at": None,
            "push_at": None,  # the user has seen it: the next round is pushed instead
            "purge_at": self._purge_at(current.due_on, remind_at),
        }
        result = await Reminder.get_pymongo_collection().update_one(
            self.query({"_id": current.id, "dismissed_at": None}),
            {"$set": {**changes, **self._stamp()}, "$inc": {"round": 1}},
        )
        if result.matched_count == 0:
            raise NotFoundError  # dismissed: there is nothing left to snooze
        return await self.get(reminder_id)

    async def dismiss(self, reminder_id: str) -> None:
        """Stop reminding; dismissing again changes nothing."""
        current = await self.get(reminder_id)
        await Reminder.get_pymongo_collection().update_one(
            {"_id": current.id, "dismissed_at": None},
            {"$set": {"dismissed_at": self.clock.now(), "push_at": None, **self._stamp()}},
        )

    async def rezone(self, previous: str) -> None:
        """The user moved from the ``previous`` time zone: each reminder not yet told keeps its
        local day and goes out at the send hour of the new zone."""
        pending = await Reminder.find(self.query({"sent_at": None, "dismissed_at": None})).to_list()
        for item in pending:
            remind_at = self._at(item.remind_at.astimezone(ZoneInfo(previous)).date())
            changes = {"remind_at": remind_at, "purge_at": self._purge_at(item.due_on, remind_at)}
            if item.snoozed_until is not None:
                changes["snoozed_until"] = remind_at
            await Reminder.get_pymongo_collection().update_one(  # unless told or snoozed since
                {"_id": item.id, "round": item.round, "sent_at": None},
                {"$set": {**changes, **self._stamp()}},
            )

    # ------------------------------------------------------------------ helpers

    def _at(self, day: date) -> datetime:
        return at_local(day, self.ctx.user.timezone, self.settings.send_hour)

    def _purge_at(self, due_on: str, remind_at: datetime) -> datetime:
        """Kept until the retention period after the due day has begun in every zone, so the
        retention guard refuses a purged due date whatever DST or a move of zone does in
        between; and for the retention period after it is told."""
        retention = timedelta(days=self.settings.retention_days)
        ended = datetime.combine(date.fromisoformat(due_on) + retention, time(), LAST_ZONE)
        return max(ended, remind_at + retention).astimezone(UTC)

    def _stamp(self) -> dict[str, Any]:
        return {"updated_at": self.clock.now(), "updated_by": self.scope.actor_id}

    async def _cancel(self, criteria: Mapping[str, Any]) -> int:
        cancelled = {"deleted_at": self.clock.now(), "push_at": None, **self._stamp()}
        result = await Reminder.get_pymongo_collection().update_many(
            self.query(criteria), {"$set": cancelled}
        )
        return result.modified_count

    async def _supersede(self, ref: Mapping[str, Any], day: str) -> None:
        """Last writer wins: cancel the record's other due dates written before this one. Two
        schedule() calls at once each cancel the others before writing, so neither sees the
        other's row; the later write cancels the earlier here."""
        mine = await Reminder.find_one(self.query({**ref, "due_on": day}))
        if mine is None:
            return  # a later schedule() has cancelled it already
        earlier = [
            {"updated_at": {"$lt": mine.updated_at}},
            {"updated_at": mine.updated_at, "_id": {"$lt": mine.id}},
        ]
        await self._cancel({**ref, "due_on": {"$ne": day}, "$or": earlier})

    async def _usable(self) -> dict[str, Any]:
        """A filter leaving out modules switched off for the user, by the rule that also
        guards their routes, widgets and tools."""
        modules = self.ctx.state.modules
        usable = await ModuleService.of(self.ctx).usable_owners()
        blocked = [m.manifest.key for m in modules if not usable(m.manifest.key)]
        return {"module_key": {"$nin": blocked}}
