"""``POST /v1/expenses/entries/duplicates/check``: saved entries that look like new ones."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.expenses.conftest import ENTRIES, add, post

CHECK = f"{ENTRIES}/duplicates/check"


def candidate(**changes: Any) -> dict[str, Any]:
    return {"title": "Rice", "amount_minor": 12500, "date": "2026-10-08"} | changes


async def check(client: AsyncClient, account: Account, *candidates: Any) -> list[list[str]]:
    """The ids found for each candidate, in the order asked."""
    response = await client.post(
        CHECK, headers=account.headers, json={"candidates": list(candidates)}
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [result["index"] for result in results] == list(range(len(candidates)))
    return [[match["id"] for match in result["matches"]] for result in results]


async def test_the_same_entry_on_a_nearby_day_is_found_and_can_still_be_saved(
    client: AsyncClient,
) -> None:
    account = await register(client)
    day_before = await add(client, account, date="2026-10-07")
    same_day = await add(client, account)
    day_after = await add(client, account, date="2026-10-09")
    await add(client, account, date="2026-10-06")  # outside the one-day window
    await add(client, account, date="2026-10-10")

    found = await check(client, account, candidate())
    again = await post(client, account)

    assert found == [[day_after["id"], same_day["id"], day_before["id"]]]  # newest first
    assert again.status_code == 201  # advice only: nothing is refused


async def test_names_match_whatever_their_case_spacing_plural_or_script(
    client: AsyncClient,
) -> None:
    account = await register(client)
    rice = await add(client, account, title="Rice  bag")
    tomatoes = await add(client, account, title="Tomatoes", amount_minor=8000)
    await add(client, account, title="Bread", amount_minor=8000)  # same amount, another thing
    dosa = await add(client, account, title="தோசை", amount_minor=6000)

    found = await check(
        client,
        account,
        candidate(title=" RICE BAG "),
        candidate(title="Tomato", amount_minor=8000),
        # The same word with its vowel sign typed in two parts (U+0BC7 U+0BBE for U+0BCB): too
        # unlike to pass as similar, so only normalising the name makes it match.
        candidate(title="தோசை", amount_minor=6000),
    )

    assert found == [[rice["id"]], [tomatoes["id"]], [dosa["id"]]]


async def test_the_same_place_matches_under_another_name(client: AsyncClient) -> None:
    account = await register(client)
    shop = await add(client, account, title="Groceries", merchant="Big Bazaar")

    found = await check(
        client,
        account,
        candidate(title="Weekly shopping", merchant="big  bazaar"),
        candidate(title="Weekly shopping"),
    )

    assert found == [[shop["id"]], []]


async def test_what_differs_or_is_not_theirs_is_not_found(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    asha, ravi = await register(client), await register(other_client)
    await add(client, asha, amount_minor=12600)
    await add(client, asha, kind="income", category_id="salary")
    gone = await add(client, asha)
    await client.delete(f"{ENTRIES}/{gone['id']}", headers=asha.headers)
    await add(other_client, ravi)
    mine = await add(client, asha, date="2026-10-09")

    found = await check(client, asha, candidate(), candidate(exclude_id=mine["id"]))

    assert found == [[mine["id"]], []]  # the entry being edited is never its own match


async def test_each_candidate_gets_its_own_answer_in_order(client: AsyncClient) -> None:
    account = await register(client)
    teas = [
        await add(client, account, title="Tea", amount_minor=2000, date=day)
        for day in ("2026-10-07", "2026-10-08", "2026-10-08", "2026-10-09")
    ]
    rice = await add(client, account)

    found = await check(
        client,
        account,
        candidate(title="Coffee", amount_minor=2000),
        candidate(title="Tea", amount_minor=2000),
        candidate(),
    )

    # At most three per candidate, newest first.
    assert found == [[], [teas[3]["id"], teas[2]["id"], teas[1]["id"]], [rice["id"]]]


async def test_days_at_the_ends_of_the_calendar_are_checked_too(client: AsyncClient) -> None:
    account = await register(client)
    first = await add(client, account, date="0001-01-01")

    found = await check(client, account, candidate(date="0001-01-01"), candidate(date="9999-12-31"))

    assert found == [[first["id"]], []]  # the window stops at the calendar's ends


async def test_the_check_needs_a_signed_in_user_and_a_sensible_batch(client: AsyncClient) -> None:
    anonymous = await client.post(CHECK, json={"candidates": [candidate()]})
    account = await register(client)

    async def status(candidates: list[dict[str, Any]]) -> int:
        response = await client.post(
            CHECK, headers=account.headers, json={"candidates": candidates}
        )
        return response.status_code

    assert anonymous.status_code == 401
    assert await status([]) == 422
    assert await status([candidate()] * 11) == 422
    assert await status([candidate(title="  ")]) == 422
    assert await status([candidate(amount_minor=0)]) == 422
