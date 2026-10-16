"""``/v1/expenses/goals`` (U1): goals funded by hand, each contribution once per key, or growing
with net savings as the analytics count them; reaching one is told once for each target."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient, Response

from app.modules.expenses.features.goals.models import Goal
from app.platform.notifications.inbox import Notification
from tests.helpers import Account, FrozenClock
from tests.modules.expenses.conftest import ENTRIES, KEY_HEADER, add, signed_up_at

GOALS = "/v1/expenses/goals"
# 8 October in Kolkata, years ahead so the notifications counted outlive the test (TTL).
NOW = datetime(2037, 10, 8, 6, tzinfo=UTC)
TODAY = "2037-10-08"


async def signed_up(client: AsyncClient, clock: FrozenClock) -> Account:
    return await signed_up_at(client, clock, NOW)


def goal(**changes: Any) -> dict[str, Any]:
    return {"name": "Bike", "target_minor": 10000} | changes


async def create(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    response = await client.post(GOALS, headers=account.headers, json=goal(**changes))
    assert response.status_code == 201, response.text
    return dict(response.json())


async def post_money(
    client: AsyncClient, account: Account, goal_id: str, key: str | None = None, **body: Any
) -> Response:
    headers = account.headers | ({KEY_HEADER: key} if key else {})
    return await client.post(f"{GOALS}/{goal_id}/contributions", headers=headers, json=body)


async def put_in(
    client: AsyncClient, account: Account, goal_id: str, amount_minor: int, **body: Any
) -> dict[str, Any]:
    response = await post_money(client, account, goal_id, amount_minor=amount_minor, **body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def listed(client: AsyncClient, account: Account) -> dict[str, dict[str, Any]]:
    """Each goal as the list shows it, by name."""
    response = await client.get(f"{GOALS}?page_size=100", headers=account.headers)
    assert response.status_code == 200, response.text
    return {item["name"]: item for item in response.json()["items"]}


async def told(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    """The "goal reached" notifications, newest first."""
    page = (await client.get("/v1/notifications?page_size=100", headers=account.headers)).json()
    return [item["params"] for item in page["items"] if item["type"] == "expenses.goal_reached"]


async def test_a_goal_funded_by_hand_adds_up_exactly(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    bike = await create(client, account, target_minor=5000000, target_date="2038-03-31")

    await put_in(client, account, bike["id"], 1234567)
    await put_in(client, account, bike["id"], 250000, date="2037-10-01")
    last = await put_in(client, account, bike["id"], -84568, note="Took some for rent")

    # October to March is six months, this one counted; 3,600,001 left is 600,001 a month.
    assert (last["saved_minor"], last["left_minor"], last["pct"]) == (1399999, 3600001, 27)
    assert (last["per_month_minor"], last["reached"]) == (600001, False)
    assert (last["funding"], last["since"], last["currency"]) == ("manual", TODAY, "INR")
    assert (await listed(client, account))["Bike"] == last


async def test_what_is_left_a_month_needs_a_date_still_to_come(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    await create(client, account, name="Someday")
    await create(client, account, name="Missed", target_date="2037-09-30")
    await create(client, account, name="This month", target_date="2037-10-31")
    reached = await create(client, account, name="Done", target_minor=100, target_date="2038-01-01")
    await put_in(client, account, reached["id"], 100)

    found = await listed(client, account)

    assert {name: item["per_month_minor"] for name, item in found.items()} == {
        "Someday": None,
        "Missed": None,
        "This month": 10000,
        "Done": None,
    }
    assert (found["Done"]["left_minor"], found["Done"]["pct"]) == (0, 100)


async def test_the_same_key_replays_a_contribution_and_a_new_goal(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    key = uuid.uuid4().hex
    made = await client.post(GOALS, headers=account.headers | {KEY_HEADER: key}, json=goal())
    again = await client.post(
        GOALS, headers=account.headers | {KEY_HEADER: key}, json=goal(name="Other body")
    )
    goal_id = made.json()["id"]

    paid = await post_money(client, account, goal_id, key, amount_minor=5000)
    replayed = await post_money(client, account, goal_id, key, amount_minor=9999)

    assert (made.status_code, again.status_code) == (201, 201)
    assert (again.json()["id"], again.json()["name"]) == (goal_id, "Bike")
    assert again.headers["Idempotent-Replayed"] == "true"
    assert (paid.status_code, replayed.status_code) == (201, 201)
    assert "Idempotent-Replayed" not in paid.headers
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert (replayed.json()["saved_minor"], replayed.json()["revision_no"]) == (5000, 2)
    assert list(await listed(client, account)) == ["Bike"]


async def test_contributions_sent_at_the_same_moment_each_land_once(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account, target_minor=100000)
    key = uuid.uuid4().hex

    apart = await asyncio.gather(
        *(
            post_money(client, account, made["id"], uuid.uuid4().hex, amount_minor=100 * n)
            for n in range(1, 6)
        )
    )
    same = await asyncio.gather(
        *(post_money(client, account, made["id"], key, amount_minor=7) for _ in range(5))
    )
    stored = await Goal.get(PydanticObjectId(made["id"]))

    assert [r.status_code for r in apart + same] == [201] * 10, [r.text for r in apart + same]
    assert stored is not None
    assert sorted(line.amount_minor for line in stored.contributions) == [
        7,
        100,
        200,
        300,
        400,
        500,
    ]
    assert (stored.saved_minor, stored.revision_no) == (1507, 7)
    assert (await listed(client, account))["Bike"]["saved_minor"] == 1507


async def test_a_contribution_must_fit_the_goal(client: AsyncClient, clock: FrozenClock) -> None:
    account = await signed_up(client, clock)
    by_hand = await create(client, account)
    kept = await create(client, account, name="Kept", funding="net_savings")
    await put_in(client, account, by_hand["id"], 3000)

    async def refused(goal_id: str, **body: Any) -> tuple[str, Any]:
        response = await post_money(client, account, goal_id, **body)
        assert response.status_code == 422, response.text
        return response.json()["code"], response.json()["details"]

    too_much = await refused(by_hand["id"], amount_minor=-3001)
    tomorrow = await refused(by_hand["id"], amount_minor=100, date="2037-10-09")
    automatic = await refused(kept["id"], amount_minor=100)
    nothing = await refused(by_hand["id"], amount_minor=0)

    code = "EXPENSE_CONTRIBUTION_INVALID"
    assert too_much == (code, {"reason": "below_zero", "saved_minor": 3000})
    assert tomorrow == (code, {"reason": "future", "saved_minor": 3000})
    assert automatic == (code, {"reason": "net_savings"})  # nothing was put in by hand
    assert nothing[0] == "VALIDATION_ERROR"
    found = await listed(client, account)
    assert (found["Bike"]["saved_minor"], found["Bike"]["revision_no"]) == (3000, 2)
    assert (await put_in(client, account, by_hand["id"], -3000))["saved_minor"] == 0


async def test_a_net_savings_goal_is_what_the_analytics_keep_by_the_users_own_days(
    client: AsyncClient, clock: FrozenClock
) -> None:
    # 00:30 on 9 October in Kolkata, still 8 October in UTC.
    account = await signed_up_at(client, clock, datetime(2037, 10, 8, 19, tzinfo=UTC))
    income = {"kind": "income", "category_id": "salary"}
    await add(client, account, title="Salary", amount_minor=5000000, date="2037-10-01", **income)
    await add(client, account, title="Bonus", amount_minor=100000, date="2037-09-30", **income)
    await add(client, account, title="Rent", amount_minor=1200000, date="2037-10-05")
    await add(client, account, title="Tea", amount_minor=300000, date="2037-10-09")  # today
    await add(client, account, title="Later", amount_minor=50000, date="2037-10-10")
    gone = await add(client, account, title="Mistake", amount_minor=70000, date="2037-10-03")
    await client.delete(f"{ENTRIES}/{gone['id']}", headers=account.headers)

    made = await create(
        client, account, funding="net_savings", since="2037-10-01", target_minor=10000000
    )
    net = await client.get(
        "/v1/expenses/analytics/net?from=2037-10-01&to=2037-10-09", headers=account.headers
    )
    await add(client, account, title="Laptop", amount_minor=4000000, date="2037-10-09")
    overspent = (await listed(client, account))["Bike"]

    assert made["saved_minor"] == net.json()["total"]["net_minor"] == 3500000
    assert (made["pct"], made["left_minor"]) == (35, 6500000)
    assert (overspent["saved_minor"], overspent["pct"]) == (0, 0)  # never below nothing


async def test_a_change_made_to_an_older_revision_is_refused(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account, target_date="2038-03-31")
    await put_in(client, account, made["id"], 500)  # revision 2

    stale = await client.patch(
        f"{GOALS}/{made['id']}", headers=account.headers, json={"name": "Car", "revision_no": 1}
    )
    current = await client.patch(
        f"{GOALS}/{made['id']}",
        headers=account.headers,
        json={"name": "Car", "target_date": None, "target_minor": 20000, "revision_no": 2},
    )
    cleared = await client.patch(
        f"{GOALS}/{made['id']}",
        headers=account.headers,
        json={"target_minor": None, "revision_no": 3},
    )

    assert stale.status_code == 409
    assert (stale.json()["code"], stale.json()["details"]) == ("VERSION_CONFLICT", {"current": 2})
    assert current.status_code == 200, current.text
    changed = current.json()
    assert (changed["name"], changed["target_date"], changed["target_minor"]) == (
        "Car",
        None,
        20000,
    )
    assert (changed["saved_minor"], changed["revision_no"]) == (500, 3)
    assert (cleared.status_code, cleared.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_reaching_a_goal_is_told_once_and_again_only_for_a_new_target(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account)
    await put_in(client, account, made["id"], 6000)
    quiet = await told(client, account)

    reached = await put_in(client, account, made["id"], 4000)
    await listed(client, account)
    await put_in(client, account, made["id"], -1000)  # out and back in: the same target
    await put_in(client, account, made["id"], 1000)
    once = await told(client, account)
    # What the TTL does after NOTIFICATIONS_RETENTION_DAYS: looking again tells nothing new.
    await Notification.get_pymongo_collection().delete_many({"params.goal_id": made["id"]})
    await listed(client, account)
    after_purge = await told(client, account)

    raised = await client.patch(
        f"{GOALS}/{made['id']}",
        headers=account.headers,
        json={"target_minor": 15000, "revision_no": 5},
    )
    await put_in(client, account, made["id"], 5000)

    assert (quiet, reached["reached"]) == ([], True)
    assert once == [
        {
            "module": "expenses",
            "goal_id": made["id"],
            "name": "Bike",
            "target_minor": 10000,
            "currency": "INR",
        }
    ]
    assert after_purge == []
    assert raised.json()["reached"] is False
    assert [params["target_minor"] for params in await told(client, account)] == [15000]


async def test_a_net_savings_goal_is_told_when_it_is_looked_at(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    await create(client, account, name="Cushion", funding="net_savings", target_minor=1000)
    await add(client, account, kind="income", category_id="salary", amount_minor=2500, date=TODAY)
    quiet = await told(client, account)  # an entry alone tells nothing

    await listed(client, account)
    await listed(client, account)

    assert quiet == []
    assert [params["name"] for params in await told(client, account)] == ["Cushion"]


async def test_a_goal_is_deleted_and_brought_back(client: AsyncClient, clock: FrozenClock) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account, name="  Bike ")
    await put_in(client, account, made["id"], 2500)

    deleted = await client.delete(f"{GOALS}/{made['id']}", headers=account.headers)
    hidden = await listed(client, account)
    paid = await post_money(client, account, made["id"], amount_minor=100)
    back = await client.post(f"{GOALS}/{made['id']}/undelete", headers=account.headers)

    assert made["name"] == "Bike"
    assert (deleted.status_code, hidden, paid.status_code) == (204, {}, 404)
    assert back.status_code == 200, back.text
    assert (back.json()["saved_minor"], back.json()["revision_no"]) == (2500, 4)
    assert list(await listed(client, account)) == ["Bike"]


@pytest.mark.parametrize(
    "changes",
    [
        {"target_minor": 0},
        {"target_minor": 10**13 + 1},
        {"name": "   "},
        {"funding": "lottery"},
        {"target_date": "someday"},
        {"owner": "someone else"},
    ],
)
async def test_a_goal_must_make_sense(
    client: AsyncClient, changes: dict[str, Any], clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)

    response = await client.post(GOALS, headers=account.headers, json=goal(**changes))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
