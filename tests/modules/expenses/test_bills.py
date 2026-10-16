"""ACCEPTANCE (Phase 9a): a bill reminds the user exactly once for each due date, however often
jobs run. Time walks 40 days in 30-minute steps, and at each step the cron runs and the bell
polls, as when the app is open while the cron fires."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, timedelta
from typing import Any

from beanie import PydanticObjectId
from httpx import AsyncClient, Response

from app.core.daterange import at_local
from app.platform.notifications.inbox import Notification
from app.platform.reminders.models import Reminder
from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.test_figures import today_of
from tests.modules.expenses.test_recurring import RECURRING, create, made_by, on, rules
from tests.platform.reminders.test_exactly_once import STEP, walk


async def told(account: Account) -> Counter[tuple[str, str]]:
    """How often each (bill, due date) was told."""
    items = await Notification.find(
        {"user_id": PydanticObjectId(account.user_id), "type": "expenses.bill_due"}
    ).to_list()
    return Counter((item.params["title"], item.params["due_on"]) for item in items)


async def test_each_bill_due_date_is_told_once_however_often_jobs_run(
    cron_client: AsyncClient, clock: FrozenClock
) -> None:
    """A monthly bill left unpaid is told once, two days before it is due, and stays due; paid
    late (twice at once: one entry), its next due date is told once. A bill paid before its
    reminder is never told for that due date."""
    client = cron_client
    account = await register(client)
    today = today_of(clock)
    start = clock.now()
    electricity = await create(client, account, on(today, 5))  # reminded two days before
    water = await create(client, account, on(today, 3), title="Water", remind_days_before=0)

    async def pay(bill: dict[str, Any], due: str) -> Response:
        url = f"{RECURRING}/{bill['id']}/mark-paid"
        return await client.post(url, headers=account.headers, json={"due_on": due})

    early = await pay(water, on(today, 3))
    await walk(client, clock, account, hours=10 * 24, times=1)
    late = await asyncio.gather(pay(electricity, on(today, 5)), pay(electricity, on(today, 5)))
    await walk(client, clock, account, hours=30 * 24, times=1)

    now = await rules(client, account)
    next_electricity, next_water = now[electricity["id"]]["next_run"], now[water["id"]]["next_run"]
    assert await told(account) == {
        ("Electricity", on(today, 5)): 1,
        ("Electricity", next_electricity): 1,
        ("Water", next_water): 1,
    }
    # Each told at the first step at or after its time: 9:00 their time, the days before.
    mine = await Reminder.find({"user_id": PydanticObjectId(account.user_id)}).to_list()
    for item in mine:
        if item.sent_at is not None:
            assert timedelta(0) <= item.sent_at - max(item.remind_at, start) < STEP
    first = next(
        item for item in mine if (item.ref_id, item.due_on) == (electricity["id"], on(today, 5))
    )
    assert first.remind_at == at_local(date.fromisoformat(on(today, 3)), "Asia/Kolkata", 9)
    # Both payments answered alike: one made the entry, the other found it.
    assert [response.status_code for response in (early, *late)] == [201, 201, 201]
    assert sorted(item.headers.get("Idempotent-Replayed", "") for item in late) == ["", "true"]
    assert await made_by(client, account, electricity["id"]) == [on(today, 10)]
    assert await made_by(client, account, water["id"]) == [on(today)]
    # Both next due dates have passed unpaid by now.
    assert (now[electricity["id"]]["overdue"], now[water["id"]]["overdue"]) == (True, True)
    assert next_electricity > on(today, 5)
    assert next_water > on(today, 3)
