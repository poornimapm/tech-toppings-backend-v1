"""ACCEPTANCE (Phase 9a): reminders fire exactly once per due date, however often and however
concurrently jobs run. Time walks in 30-minute steps over three days (2-hour steps where a test
only needs time to pass); at each step the cron runs twice and the bell polls twice, as when
the app is open while the cron fires.

The cron reads across users, so each test has a database of its own."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import JobsSettings, Settings
from app.core.daterange import at_local
from app.platform.notifications.inbox import Notification
from app.platform.reminders.models import Reminder
from app.platform.reminders.service import Reminders
from app.platform.users.models import User
from tests.helpers import (
    Account,
    FrozenClock,
    actor_of,
    build_app,
    build_settings,
    register,
    running_client,
)

TOKEN = "cron-" + "t" * 43
STEP = timedelta(minutes=30)
LONG_STEP = timedelta(hours=2)  # where a test only needs time to pass


@pytest.fixture
def settings(own_mongo_uri: str) -> Settings:
    return build_settings(own_mongo_uri, jobs=JobsSettings(_env_file=None, token=SecretStr(TOKEN)))


async def busy_moment(client: AsyncClient, account: Account, times: int = 2) -> None:
    """The cron and the bell, each ``times`` times."""
    for _ in range(times):
        cron = await client.post("/v1/internal/jobs/all/run", headers={"X-Job-Token": TOKEN})
        assert cron.status_code == 200, cron.text
    for _ in range(times):
        bell = await client.get(
            "/v1/notifications?unread=true&page_size=1", headers=account.headers
        )
        assert bell.status_code == 200, bell.text


async def walk(
    client: AsyncClient,
    clock: FrozenClock,
    account: Account,
    hours: float,
    every: timedelta = STEP,
    times: int = 2,
) -> None:
    """A busy moment every ``every``, for ``hours``."""
    end = clock.now() + timedelta(hours=hours)
    while clock.now() < end:
        await busy_moment(client, account, times)
        clock.advance(seconds=every.total_seconds())


async def told(account: Account) -> Counter[tuple[str, str]]:
    """How often each (record, due date) was told."""
    items = await Notification.find(
        {"user_id": PydanticObjectId(account.user_id), "type": "notes.due"}
    ).to_list()
    return Counter((item.params["title"], item.params["due_on"]) for item in items)


class Notes:
    """The notes module's side: it reminds of a note's due date."""

    def __init__(self, reminders: Reminders) -> None:
        self.reminders = reminders

    async def due(self, ref: str, due_on: date, days_before: int = 0) -> None:
        await self.reminders.schedule(
            "notes.due",
            module="notes",
            ref_id=ref,
            due_on=due_on,
            days_before=days_before,
            title=ref,
        )


async def notes_of(app: FastAPI, account: Account) -> tuple[Notes, date]:
    ctx = await actor_of(app, account)
    return Notes(Reminders(ctx)), ctx.today()


async def reminder_ids(client: AsyncClient, account: Account) -> dict[str, str]:
    page = (await client.get("/v1/reminders", headers=account.headers)).json()
    return {item["params"]["title"]: item["id"] for item in page["items"]}


async def test_each_due_date_is_told_once_however_often_jobs_run(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    plan = {
        "overdue": (today - timedelta(days=2), 0),
        "today": (today, 0),
        "tomorrow": (today + timedelta(days=1), 0),
        "early": (today + timedelta(days=3), 1),  # told the day before
        "later": (today + timedelta(days=9), 0),  # not yet
    }
    for ref, (due_on, before) in plan.items():
        await notes.due(ref, due_on, before)
    start = clock.now()

    await walk(client, clock, account, hours=72)

    expected = {(ref, due_on.isoformat()): 1 for ref, (due_on, _) in plan.items()}
    del expected[("later", plan["later"][0].isoformat())]
    assert await told(account) == expected
    for item in await Reminder.find({"user_id": PydanticObjectId(account.user_id)}).to_list():
        if item.sent_at is not None:  # told at the first step at or after its time
            assert timedelta(0) <= item.sent_at - max(item.remind_at, start) < STEP


async def test_concurrent_deliveries_tell_a_due_date_once(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    ctx = await actor_of(app, account)
    await Notes(Reminders(ctx)).due("bill", ctx.today() - timedelta(days=1))

    counts = await asyncio.gather(*(Reminders(ctx).deliver_due() for _ in range(10)))

    assert sum(counts) == 1
    assert sum((await told(account)).values()) == 1


async def test_a_crash_between_telling_and_marking_still_tells_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    await notes.due("bill", today - timedelta(days=1))
    mark_sent, crashes = Reminders._mark_sent, list[object]()

    async def crash_once(self: Reminders, item: Reminder, *rest: Any) -> int:
        if not crashes:
            crashes.append(item.id)
            raise RuntimeError("the process stopped here")
        return await mark_sent(self, item, *rest)

    monkeypatch.setattr(Reminders, "_mark_sent", crash_once)

    await walk(client, clock, account, hours=1)

    assert crashes
    assert sum((await told(account)).values()) == 1
    (item,) = await Reminder.find({"user_id": PydanticObjectId(account.user_id)}).to_list()
    assert item.sent_at is not None


async def test_scheduling_a_told_due_date_again_does_not_tell_it_again(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    due_on = today - timedelta(days=1)

    await notes.due("bill", due_on)
    await walk(client, clock, account, hours=0.5)
    await notes.due("bill", due_on)  # e.g. the record was saved again
    await walk(client, clock, account, hours=24, every=LONG_STEP)

    assert await told(account) == {("bill", due_on.isoformat()): 1}


async def test_moving_a_due_date_tells_the_new_one_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    first, moved = today - timedelta(days=1), today + timedelta(days=1)
    unsent, earlier = today + timedelta(days=5), today - timedelta(days=1)

    await notes.due("bill", first)
    await notes.due("loan", unsent)
    await walk(client, clock, account, hours=0.5)
    await notes.due("bill", moved)  # paid: the next one is due
    await notes.due("loan", earlier)  # moved before it was ever told
    await walk(client, clock, account, hours=48, every=LONG_STEP)

    assert await told(account) == {
        ("bill", first.isoformat()): 1,
        ("bill", moved.isoformat()): 1,
        ("loan", earlier.isoformat()): 1,
    }


async def test_a_snooze_tells_exactly_once_more(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    await notes.due("bill", today - timedelta(days=1))
    await walk(client, clock, account, hours=0.5)
    reminder_id = (await reminder_ids(client, account))["bill"]

    snoozed = await client.post(
        f"/v1/reminders/{reminder_id}/snooze", headers=account.headers, json={"days": 1}
    )
    await walk(client, clock, account, hours=48, every=LONG_STEP)

    assert snoozed.status_code == 200, snoozed.text
    assert await told(account) == {("bill", (today - timedelta(days=1)).isoformat()): 2}
    again = await Notification.find(
        {"user_id": PydanticObjectId(account.user_id), "params.reminder_id": reminder_id}
    ).to_list()
    assert sorted(item.dedupe_key or "" for item in again) == [
        f"reminder:{reminder_id}:0",
        f"reminder:{reminder_id}:1",
    ]


async def test_dismissed_or_cancelled_reminders_are_never_told(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    ctx = await actor_of(app, account)
    notes = Notes(Reminders(ctx))
    tomorrow = ctx.today() + timedelta(days=1)
    await notes.due("dismissed", tomorrow)
    await notes.due("cancelled", tomorrow)

    dismissed = await client.post(
        f"/v1/reminders/{(await reminder_ids(client, account))['dismissed']}/dismiss",
        headers=account.headers,
    )
    cancelled = await Reminders(ctx).cancel(module="notes", ref_id="cancelled")
    await walk(client, clock, account, hours=48, every=LONG_STEP)

    assert (dismissed.status_code, cancelled) == (204, 1)
    assert await told(account) == {}


async def test_a_switched_off_module_waits_then_tells_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    await notes.due("bill", today - timedelta(days=1))
    preferences = "/v1/modules/notes/preferences"

    await client.patch(preferences, headers=account.headers, json={"enabled": False})
    await walk(client, clock, account, hours=1)
    waiting = await told(account), await reminder_ids(client, account)
    await client.patch(preferences, headers=account.headers, json={"enabled": True})
    await walk(client, clock, account, hours=1)

    assert waiting == (Counter(), {})  # not told, and not listed
    assert sum((await told(account)).values()) == 1


async def test_a_due_date_past_retention_is_refused(
    app: FastAPI, client: AsyncClient, clock: FrozenClock, settings: Settings
) -> None:
    account = await register(client)
    notes, today = await notes_of(app, account)
    too_old = today - timedelta(days=settings.reminders.retention_days)
    kept = too_old + timedelta(days=1)

    await notes.due("too old", too_old)  # its row could already be purged
    await notes.due("kept", kept)
    await walk(client, clock, account, hours=0.5)

    assert await told(account) == {("kept", kept.isoformat()): 1}


@pytest.mark.parametrize(
    ("scheduled_in", "later_in"),
    [
        ("America/New_York", "America/New_York"),  # the clocks go back in November
        ("Asia/Kolkata", "Pacific/Pago_Pago"),  # the user moves west, to UTC-11
    ],
)
async def test_a_purged_due_date_is_never_told_again(
    settings: Settings, scheduled_in: str, later_in: str
) -> None:
    # Years ahead, so the database's own TTL never removes what the test counts.
    clock = FrozenClock(datetime(2030, 9, 14, 12, tzinfo=UTC))
    due_on = date(2030, 9, 15)
    last_day = due_on + timedelta(days=settings.reminders.retention_days - 1)
    app = build_app(settings, clock)
    async with running_client(app) as http:
        account = await register(http)
        users = User.get_pymongo_collection()
        user = {"_id": PydanticObjectId(account.user_id)}
        await users.update_one(user, {"$set": {"timezone": scheduled_in}})
        ctx = await actor_of(app, account)
        await Notes(Reminders(ctx)).due("bill", due_on, days_before=2)
        await Reminders(ctx).deliver_due()

        # Half an hour before the guard refuses that due date, after TTL purged what was due.
        await users.update_one(user, {"$set": {"timezone": later_in}})
        moment = at_local(last_day + timedelta(days=1), later_in) - STEP
        clock.advance(seconds=(moment - clock.now()).total_seconds())
        await Reminder.get_pymongo_collection().delete_many({"purge_at": {"$lte": clock.now()}})
        ctx = await actor_of(app, account)
        await Notes(Reminders(ctx)).due("bill", due_on, days_before=2)
        await Reminders(ctx).deliver_due()

        assert ctx.today() == last_day
        assert await told(account) == {("bill", due_on.isoformat()): 1}


async def test_concurrent_schedules_leave_a_record_one_live_reminder(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    ctx = await actor_of(app, account)
    today = ctx.today()

    for n in range(30):
        await asyncio.gather(
            Notes(Reminders(ctx)).due(f"bill {n}", today + timedelta(days=3)),
            Notes(Reminders(ctx)).due(f"bill {n}", today + timedelta(days=4)),
        )

    live = await Reminder.find(
        {"user_id": PydanticObjectId(account.user_id), "deleted_at": None}
    ).to_list()
    assert Counter(item.ref_id for item in live) == {f"bill {n}": 1 for n in range(30)}


@pytest.mark.parametrize("name", ["dedupe_key", "reminder_id"])
async def test_reserved_params_are_refused(app: FastAPI, client: AsyncClient, name: str) -> None:
    ctx = await actor_of(app, await register(client))
    params: dict[str, Any] = {name: "mine"}

    with pytest.raises(ValueError, match=name):
        await Reminders(ctx).schedule(
            "notes.due", module="notes", ref_id="a", due_on=ctx.today(), **params
        )


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        # 9:00 local on 30 October and on 2 November 2026; New York leaves DST on 1 November.
        (
            "Asia/Kolkata",
            [datetime(2026, 10, 30, 3, 30, tzinfo=UTC), datetime(2026, 11, 2, 3, 30, tzinfo=UTC)],
        ),
        (
            "America/New_York",
            [datetime(2026, 10, 30, 13, tzinfo=UTC), datetime(2026, 11, 2, 14, tzinfo=UTC)],
        ),
    ],
)
async def test_reminders_go_out_at_the_users_local_send_hour(
    settings: Settings, timezone: str, expected: list[datetime]
) -> None:
    clock = FrozenClock(datetime(2026, 10, 29, tzinfo=UTC))
    app = build_app(settings, clock)
    async with running_client(app) as http:
        account = await register(http)
        await User.get_pymongo_collection().update_one(
            {"_id": PydanticObjectId(account.user_id)}, {"$set": {"timezone": timezone}}
        )
        ctx = await actor_of(app, account)
        notes = Notes(Reminders(ctx))
        for day in (date(2026, 10, 30), date(2026, 11, 2)):
            await notes.due(day.isoformat(), day)
        while clock.now() < datetime(2026, 11, 3, tzinfo=UTC):
            await Reminders(ctx).deliver_due()
            clock.advance(minutes=30)
        sent = await Reminder.find({"user_id": PydanticObjectId(account.user_id)}).to_list()

    assert sorted(item.remind_at for item in sent) == expected
    assert sorted(item.sent_at for item in sent if item.sent_at) == expected
