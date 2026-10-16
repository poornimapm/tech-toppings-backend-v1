"""``/v1/expenses/ious`` (U2): money lent or borrowed, paid back in keyed lines that never take
what is paid past the amount or below nothing, balances by person, and a reminder of each due
date that is told exactly once."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import date, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient, Response

from app.modules.expenses.features.ious.models import Iou
from app.platform.notifications.inbox import Notification
from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import KEY_HEADER, listed
from tests.modules.expenses.test_figures import today_of
from tests.platform.reminders.test_exactly_once import LONG_STEP, walk

IOUS = "/v1/expenses/ious"


def iou(**changes: Any) -> dict[str, Any]:
    return {"counterparty": "Ravi", "direction": "lent", "amount_minor": 100000} | changes


async def create(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    response = await client.post(IOUS, headers=account.headers, json=iou(**changes))
    assert response.status_code == 201, response.text
    return dict(response.json())


async def pay(
    client: AsyncClient, account: Account, iou_id: str, key: str | None = None, **body: Any
) -> Response:
    headers = account.headers | ({KEY_HEADER: key} if key else {})
    return await client.post(f"{IOUS}/{iou_id}/settlements", headers=headers, json=body)


async def paid(
    client: AsyncClient, account: Account, iou_id: str, amount_minor: int, **body: Any
) -> dict[str, Any]:
    response = await pay(client, account, iou_id, amount_minor=amount_minor, **body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def loans(client: AsyncClient, account: Account, query: str = "") -> list[dict[str, Any]]:
    response = await client.get(f"{IOUS}?page_size=100{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def reminded(client: AsyncClient, account: Account) -> list[str]:
    """The people the user's open reminders are about."""
    page = (await client.get("/v1/reminders", headers=account.headers)).json()
    return [item["params"]["person"] for item in page["items"]]


async def test_the_same_key_replays_a_new_loan(client: AsyncClient) -> None:
    account = await register(client)
    key = uuid.uuid4().hex
    made = await client.post(IOUS, headers=account.headers | {KEY_HEADER: key}, json=iou())
    again = await client.post(
        IOUS, headers=account.headers | {KEY_HEADER: key}, json=iou(counterparty="Other body")
    )

    assert (made.status_code, again.status_code) == (201, 201)
    assert "Idempotent-Replayed" not in made.headers
    assert again.headers["Idempotent-Replayed"] == "true"
    assert (again.json()["id"], again.json()["counterparty"]) == (made.json()["id"], "Ravi")
    assert [item["id"] for item in await loans(client, account)] == [made.json()["id"]]


async def test_paying_in_part_then_in_full_settles_it(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    lent, yesterday = ((today_of(clock) - timedelta(days=days)).isoformat() for days in (3, 1))
    made = await create(
        client, account, counterparty="  Ravi ", date=lent, note="For the bike repair"
    )

    part = await paid(client, account, made["id"], 40000)
    full = await paid(client, account, made["id"], 60000, date=yesterday, note="Cash")

    assert (made["counterparty"], made["counterparty_key"], made["date"]) == ("Ravi", "ravi", lent)
    assert (part["settled_minor"], part["outstanding_minor"], part["settled_on"]) == (
        40000,
        60000,
        None,
    )
    assert (full["outstanding_minor"], full["settled_on"], full["revision_no"]) == (0, yesterday, 3)
    assert [(line["amount_minor"], line["note"]) for line in full["settlements"]] == [
        (40000, None),
        (60000, "Cash"),
    ]
    assert await loans(client, account) == []  # open ones by default
    assert [item["id"] for item in await loans(client, account, "&state=settled")] == [made["id"]]
    # A loan is not an entry: nothing was spent or received.
    assert (await listed(client, account))["total"] == 0


async def test_a_payment_must_fit_the_loan(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    made = await create(client, account)  # lent today, by default
    await paid(client, account, made["id"], 30000)
    today = today_of(clock)
    tomorrow, yesterday = (today + timedelta(days=days) for days in (1, -1))

    async def refused(**body: Any) -> tuple[str, Any]:
        response = await pay(client, account, made["id"], **body)
        assert response.status_code == 422, response.text
        return response.json()["code"], response.json()["details"]

    code = "EXPENSE_SETTLEMENT_INVALID"
    assert await refused(amount_minor=70001) == (
        code,
        {"reason": "too_much", "outstanding_minor": 70000},
    )
    assert await refused(amount_minor=-30001) == (
        code,
        {"reason": "below_zero", "outstanding_minor": 70000},
    )
    assert await refused(amount_minor=100, date=tomorrow.isoformat()) == (
        code,
        {"reason": "future", "outstanding_minor": 70000},
    )
    assert await refused(amount_minor=100, date=yesterday.isoformat()) == (
        code,
        {"reason": "before_loan", "outstanding_minor": 70000},
    )
    assert (await refused(amount_minor=0))[0] == "VALIDATION_ERROR"
    assert made["date"] == today.isoformat()
    (stored,) = await loans(client, account)
    assert (stored["settled_minor"], stored["revision_no"]) == (30000, 2)


async def test_the_same_payment_key_replays(client: AsyncClient) -> None:
    account = await register(client)
    made = await create(client, account)
    key = uuid.uuid4().hex

    first = await pay(client, account, made["id"], key, amount_minor=25000)
    again = await pay(client, account, made["id"], key, amount_minor=99999)

    assert (first.status_code, again.status_code) == (201, 201)
    assert "Idempotent-Replayed" not in first.headers
    assert again.headers["Idempotent-Replayed"] == "true"
    assert (again.json()["settled_minor"], again.json()["revision_no"]) == (25000, 2)


async def test_payments_sent_at_the_same_moment_never_go_past_the_amount(
    client: AsyncClient,
) -> None:
    account = await register(client)
    made = await create(client, account)

    answers = await asyncio.gather(
        *(pay(client, account, made["id"], uuid.uuid4().hex, amount_minor=30000) for _ in range(5))
    )
    stored = await Iou.get(PydanticObjectId(made["id"]))

    assert sorted(r.status_code for r in answers) == [201, 201, 201, 422, 422], [
        r.text for r in answers
    ]
    assert stored is not None
    assert [line.amount_minor for line in stored.settlements] == [30000] * 3
    assert (stored.settled_minor, stored.settled_on, stored.revision_no) == (90000, None, 4)


async def test_a_correction_reopens_a_settled_loan_and_reminds_again(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    due = (today_of(clock) + timedelta(days=10)).isoformat()
    made = await create(client, account, due_on=due)
    waiting = await reminded(client, account)

    await paid(client, account, made["id"], 100000)
    settled = await reminded(client, account)
    reopened = await paid(client, account, made["id"], -40000, note="Counted wrong")

    assert (waiting, settled) == (["Ravi"], [])
    assert (reopened["settled_on"], reopened["outstanding_minor"]) == (None, 40000)
    assert await reminded(client, account) == ["Ravi"]
    page = (await client.get("/v1/reminders", headers=account.headers)).json()
    assert page["items"][0]["params"] | {"reminder_id": None} == {
        "module": "expenses",
        "reminder_id": None,
        "due_on": due,
        "iou_id": made["id"],
        "person": "Ravi",
        "outstanding_minor": 40000,
        "currency": "INR",
    }


async def test_a_loan_is_changed_as_of_the_revision_edited_and_never_below_what_was_paid(
    client: AsyncClient,
) -> None:
    account = await register(client)
    made = await create(client, account)
    await paid(client, account, made["id"], 30000)  # revision 2
    url = f"{IOUS}/{made['id']}"

    stale = await client.patch(url, headers=account.headers, json={"note": "x", "revision_no": 1})
    below = await client.patch(
        url, headers=account.headers, json={"amount_minor": 29999, "revision_no": 2}
    )
    exact = await client.patch(
        url,
        headers=account.headers,
        json={"amount_minor": 30000, "direction": "borrowed", "revision_no": 2},
    )
    cleared = await client.patch(
        url, headers=account.headers, json={"counterparty": None, "revision_no": 3}
    )

    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")
    assert below.status_code == 422
    assert (below.json()["code"], below.json()["details"]) == (
        "EXPENSE_IOU_BELOW_SETTLED",
        {"settled_minor": 30000},
    )
    assert exact.status_code == 200, exact.text
    changed = exact.json()
    assert (changed["direction"], changed["outstanding_minor"]) == ("borrowed", 0)
    assert changed["settled_on"] == changed["settlements"][-1]["date"]
    assert (cleared.status_code, cleared.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_the_summary_adds_up_each_person_however_the_name_is_written(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    yesterday, soon = today - timedelta(days=1), today + timedelta(days=3)
    await create(client, account, counterparty="ravi ", amount_minor=50000, due_on=soon.isoformat())
    lent = await create(client, account, counterparty="Ravi", amount_minor=100000)
    await create(client, account, counterparty="RAVI", direction="borrowed", amount_minor=30000)
    await create(
        client,
        account,
        counterparty="Meena",
        direction="borrowed",
        amount_minor=20000,
        date=(yesterday - timedelta(days=5)).isoformat(),
        due_on=yesterday.isoformat(),
    )
    priya = await create(client, account, counterparty="Priya", amount_minor=7000)
    await paid(client, account, priya["id"], 7000)
    await paid(client, account, lent["id"], 25000)
    gone = await create(client, account, counterparty="Ghost", amount_minor=999)
    await client.delete(f"{IOUS}/{gone['id']}", headers=account.headers)

    summary = (await client.get(f"{IOUS}/summary", headers=account.headers)).json()
    ravis = await loans(client, account, "&person=RAVI")

    assert (summary["currency"], summary["owed_to_me_minor"], summary["i_owe_minor"]) == (
        "INR",
        125000,
        50000,
    )
    people = [
        (p["name"], p["key"], p["owed_to_me_minor"], p["i_owe_minor"], p["net_minor"], p["open"])
        for p in summary["people"]
    ]
    # Open first, the soonest due first (Meena is overdue); then those all paid back.
    assert people == [
        ("Meena", "meena", 0, 20000, -20000, 1),
        ("RAVI", "ravi", 125000, 30000, 95000, 3),
        ("Priya", "priya", 0, 0, 0, 0),
    ]
    assert [(p["next_due_on"], p["overdue"]) for p in summary["people"]] == [
        (yesterday.isoformat(), True),
        (soon.isoformat(), False),
        (None, False),
    ]
    assert len(ravis) == 3


async def test_a_loan_is_deleted_and_brought_back(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    due = (today_of(clock) + timedelta(days=5)).isoformat()
    made = await create(client, account, due_on=due)
    url = f"{IOUS}/{made['id']}"
    one = await client.get(url, headers=account.headers)

    deleted = await client.delete(url, headers=account.headers)
    hidden = await loans(client, account, "&state=all"), await reminded(client, account)
    gone = await client.get(url, headers=account.headers)
    refused = await pay(client, account, made["id"], amount_minor=100)
    back = await client.post(f"{url}/undelete", headers=account.headers)

    assert (one.status_code, one.json()) == (200, made)
    assert (deleted.status_code, hidden) == (204, ([], []))
    assert (gone.status_code, refused.status_code) == (404, 404)
    assert back.status_code == 200, back.text
    assert (back.json()["revision_no"], back.json()["due_on"]) == (3, due)
    assert await reminded(client, account) == ["Ravi"]


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 0},
        {"amount_minor": 10**13 + 1},
        {"counterparty": "   "},
        {"counterparty": "x" * 61},
        {"direction": "gifted"},
        {"date": "2026-10-08", "due_on": "2026-10-07"},  # due before it was lent
        {"owner": "someone else"},
    ],
)
async def test_a_loan_must_make_sense(client: AsyncClient, changes: dict[str, Any]) -> None:
    account = await register(client)

    response = await client.post(IOUS, headers=account.headers, json=iou(**changes))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------ acceptance


async def told(account: Account) -> Counter[tuple[str, str]]:
    """How often each (person, due date) was told."""
    items = await Notification.find(
        {"user_id": PydanticObjectId(account.user_id), "type": {"$regex": "^expenses.iou_due_"}}
    ).to_list()
    return Counter((item.params["person"], item.params["due_on"]) for item in items)


async def test_each_due_date_is_told_once_however_often_jobs_run(
    cron_client: AsyncClient, clock: FrozenClock
) -> None:
    """ACCEPTANCE (Phase 9a): the reminder of a loan's due date is told exactly once, the day
    before at the send hour, under repeated cron and lazy runs; paying it back or deleting it
    first means it is never told; moving its due date tells the new one once."""
    client = cron_client
    account = await register(client)
    today = today_of(clock)
    due, moved = today + timedelta(days=2), today + timedelta(days=1)

    def on(day: date) -> str:
        return day.isoformat()

    ravi = await create(client, account, counterparty="Ravi", due_on=on(due))
    meena = await create(client, account, counterparty="Meena", due_on=on(due))
    anand = await create(
        client, account, counterparty="Anand", direction="borrowed", due_on=on(due)
    )
    # Due a day after Ravi's, so its first reminder would come inside the walk if it were kept.
    priya = await create(client, account, counterparty="Priya", due_on=on(due + timedelta(days=1)))
    await paid(client, account, meena["id"], 100000)  # paid back before it was due
    await client.delete(f"{IOUS}/{anand['id']}", headers=account.headers)
    await client.patch(
        f"{IOUS}/{priya['id']}",
        headers=account.headers,
        json={"due_on": on(moved), "revision_no": 1},
    )

    await walk(client, clock, account, hours=36)
    await paid(client, account, ravi["id"], 20000)  # after it was told: scheduled again
    await walk(client, clock, account, hours=48, every=LONG_STEP)

    assert await told(account) == {("Ravi", on(due)): 1, ("Priya", on(moved)): 1}
    types = await Notification.find(
        {"user_id": PydanticObjectId(account.user_id), "params.person": "Ravi"}
    ).to_list()
    assert [(item.type, item.params["outstanding_minor"]) for item in types] == [
        ("expenses.iou_due_lent", 100000)
    ]
