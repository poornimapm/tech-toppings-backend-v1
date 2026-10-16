"""``/v1/reminders``: my open reminders, soonest due first and only for modules I have on, and
snooze and dismiss."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.core.daterange import at_local
from app.platform.reminders.service import Reminders
from tests.helpers import Account, actor_of, register

KOLKATA, NEW_YORK = "Asia/Kolkata", "America/New_York"


def send_time(day: date, timezone: str = KOLKATA) -> str:
    """9:00 on ``day`` in ``timezone``, as the API writes it."""
    return at_local(day, timezone, 9).isoformat().replace("+00:00", "Z")


async def due(app: FastAPI, account: Account, ref: str, days: int, before: int = 0) -> date:
    """The notes module reminds of ``ref``, due ``days`` from today."""
    ctx = await actor_of(app, account)
    due_on = ctx.today() + timedelta(days=days)
    await Reminders(ctx).schedule(
        "notes.due", module="notes", ref_id=ref, due_on=due_on, days_before=before, title=ref
    )
    return due_on


async def listed(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    response = await client.get("/v1/reminders", headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def test_lists_open_reminders_soonest_due_first(app: FastAPI, client: AsyncClient) -> None:
    account = await register(client)
    await due(app, account, "later", 5)
    soon = await due(app, account, "soon", 2)
    await due(app, account, "soon, told early", 2, before=1)
    await due(app, account, "far, told early", 6, before=5)  # told as early, but due last
    await due(app, account, "dismissed", 1)
    await due(app, account, "cancelled", 1)
    ctx = await actor_of(app, account)
    await Reminders(ctx).cancel(module="notes", ref_id="cancelled")
    dismissed = next(
        i for i in await listed(client, account) if i["params"]["title"] == "dismissed"
    )
    await client.post(f"/v1/reminders/{dismissed['id']}/dismiss", headers=account.headers)

    items = await listed(client, account)

    assert [item["params"]["title"] for item in items] == [
        "soon, told early",
        "soon",
        "later",
        "far, told early",
    ]
    first = items[1]
    assert first == {
        "id": first["id"],
        "type": "notes.due",
        "params": {
            "module": "notes",
            "reminder_id": first["id"],
            "due_on": soon.isoformat(),
            "title": "soon",
        },
        "due_on": soon.isoformat(),
        "remind_at": send_time(soon),
        "sent": False,
        "snoozed_until": None,
    }


async def test_a_module_i_switched_off_lists_none_of_its_reminders(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    await due(app, account, "bill", 1)
    await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": False}
    )

    response = await client.get("/v1/reminders", headers=account.headers)

    assert (response.json()["items"], response.json()["total"]) == ([], 0)


async def test_a_snooze_reminds_again_days_from_today(app: FastAPI, client: AsyncClient) -> None:
    account = await register(client)
    await due(app, account, "bill", -1)
    (told,) = await listed(client, account)  # listing runs due jobs, so it has been told

    response = await client.post(
        f"/v1/reminders/{told['id']}/snooze", headers=account.headers, json={"days": 3}
    )

    today = (await actor_of(app, account)).today()
    again = send_time(today + timedelta(days=3))
    assert told["sent"] is True
    assert response.status_code == 200, response.text
    assert {key: response.json()[key] for key in ("sent", "remind_at", "snoozed_until")} == {
        "sent": False,
        "remind_at": again,
        "snoozed_until": again,
    }


async def test_a_snooze_is_a_whole_number_of_days_up_to_the_limit(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    await due(app, account, "bill", 1)
    (item,) = await listed(client, account)
    url = f"/v1/reminders/{item['id']}/snooze"

    none = await client.post(url, headers=account.headers, json={"days": 0})
    too_long = await client.post(url, headers=account.headers, json={"days": 31})

    assert (none.status_code, too_long.status_code) == (422, 422)
    assert (too_long.json()["code"], too_long.json()["details"]) == (
        "VALIDATION_ERROR",
        {"max_days": 30},
    )


async def test_dismissing_is_idempotent_and_ends_snoozing(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    await due(app, account, "bill", 1)
    (item,) = await listed(client, account)
    base = f"/v1/reminders/{item['id']}"

    first = await client.post(f"{base}/dismiss", headers=account.headers)
    again = await client.post(f"{base}/dismiss", headers=account.headers)
    snooze = await client.post(f"{base}/snooze", headers=account.headers, json={"days": 1})

    assert (first.status_code, again.status_code, snooze.status_code) == (204, 204, 404)
    assert await listed(client, account) == []


async def test_a_dismissed_due_date_stays_dismissed_when_scheduled_again(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    await due(app, account, "bill", 1)
    (item,) = await listed(client, account)
    await client.post(f"/v1/reminders/{item['id']}/dismiss", headers=account.headers)

    await due(app, account, "bill", 1)  # e.g. the record was saved again

    assert await listed(client, account) == []


async def test_a_paid_or_moved_record_leaves_the_list_even_once_told(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    await due(app, account, "paid", -1)
    await due(app, account, "moved", -1)
    told = await listed(client, account)  # listing runs due jobs, so both have been told
    ctx = await actor_of(app, account)

    await Reminders(ctx).cancel(module="notes", ref_id="paid")
    moved = await due(app, account, "moved", 3)

    items = await listed(client, account)
    assert [item["sent"] for item in told] == [True, True]
    assert [(item["params"]["title"], item["due_on"]) for item in items] == [
        ("moved", moved.isoformat())
    ]


async def test_scheduling_a_snoozed_due_date_again_keeps_the_snooze(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    await due(app, account, "bill", -1)
    (told,) = await listed(client, account)
    snoozed = await client.post(
        f"/v1/reminders/{told['id']}/snooze", headers=account.headers, json={"days": 3}
    )

    await due(app, account, "bill", -1)  # e.g. the record was saved again

    (item,) = await listed(client, account)  # it would be told now if its time were reset
    again = snoozed.json()["remind_at"]
    assert {key: item[key] for key in ("sent", "remind_at", "snoozed_until")} == {
        "sent": False,
        "remind_at": again,
        "snoozed_until": again,
    }


async def test_moving_zone_keeps_each_untold_reminder_on_its_day_at_the_send_hour(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    soon = await due(app, account, "soon", 2)
    await due(app, account, "told", -1)
    await due(app, account, "snoozed", 5)
    before = {item["params"]["title"]: item for item in await listed(client, account)}
    await client.post(
        f"/v1/reminders/{before['snoozed']['id']}/snooze", headers=account.headers, json={"days": 3}
    )
    snoozed_on = (await actor_of(app, account)).today() + timedelta(days=3)

    moved = await client.patch("/v1/me", headers=account.headers, json={"timezone": NEW_YORK})

    after = {item["params"]["title"]: item for item in await listed(client, account)}
    assert moved.status_code == 200, moved.text
    assert after["soon"]["remind_at"] == send_time(soon, NEW_YORK)
    assert after["snoozed"]["remind_at"] == send_time(snoozed_on, NEW_YORK)
    assert after["snoozed"]["snoozed_until"] == send_time(snoozed_on, NEW_YORK)
    assert after["told"]["remind_at"] == before["told"]["remind_at"]  # told: left as it was


async def test_unknown_reminders_are_not_found(client: AsyncClient) -> None:
    account = await register(client)

    for action, body in (("snooze", {"days": 1}), ("dismiss", None)):
        response = await client.post(
            f"/v1/reminders/{'0' * 24}/{action}", headers=account.headers, json=body
        )
        assert response.status_code == 404, action
