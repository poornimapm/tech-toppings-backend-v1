"""``Reminders(ctx)``: how a module reminds the user of a due date, and how due reminders
reach the inbox.

Each due date is told exactly once, and that never rests on a lock or on the schedule:

* there is one row per (module, record, due date), and a row already sent or dismissed is
  never re-armed;
* a round is first told with the inbox's ``dedupe_key`` ``reminder:{id}:{round}`` and only then
  marked sent by a guarded update, so a crash in between just repeats a notify that is deduped.

A snooze starts the next round, which is told once more.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from beanie import SortDirection
from fastapi import status
from pymongo.errors import DuplicateKeyError

from app.core.daterange import at_local
from app.core.documents import OwnedDocument
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.pagination import PageParams
from app.core.repository import ScopedRepository
from app.platform.context import ActorContext
from app.platform.modules.service import ModuleService
from app.platform.notifications.inbox import Inbox
from app.platform.reminders.models import Reminder

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
        told = 0
        for item in due:
            await inbox.notify(
                item.type,
                module=item.module_key,
                dedupe_key=f"reminder:{item.id}:{item.round}",
                **notice_params(item),
            )
            told += await self._mark_sent(item)
        return told

    async def _mark_sent(self, item: Reminder) -> int:
        """Only the round just told: a snooze in the meantime has started the next one."""
        result = await Reminder.get_pymongo_collection().update_one(
            {"_id": item.id, "sent_at": None, "round": item.round},
            {"$set": {"sent_at": self.clock.now(), **self._stamp()}},
        )
        return result.modified_count

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
            {"$set": {"dismissed_at": self.clock.now(), **self._stamp()}},
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
        result = await Reminder.get_pymongo_collection().update_many(
            self.query(criteria), {"$set": {"deleted_at": self.clock.now(), **self._stamp()}}
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
        usable = await ModuleService(
            modules, self.scope, self.clock, self.ctx.settings
        ).usable_owners()
        blocked = [m.manifest.key for m in modules if not usable(m.manifest.key)]
        return {"module_key": {"$nin": blocked}}
