"""``/v1/expenses/products``: what the user buys, grouped by name, and how its price goes."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.expenses.conftest import ENTRIES, add, save

PRODUCTS = "/v1/expenses/products"


async def bought(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    return await add(client, account, category_id="groceries", **changes)


async def products(client: AsyncClient, account: Account, query: str = "") -> list[dict[str, Any]]:
    response = await client.get(f"{PRODUCTS}?page_size=100{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def detail(client: AsyncClient, account: Account, product_id: str, query: str = "") -> Any:
    response = await client.get(f"{PRODUCTS}/{product_id}{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def named(client: AsyncClient, account: Account, name: str) -> dict[str, Any]:
    return next(item for item in await products(client, account) if item["name"] == name)


async def alerts(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    response = await client.get("/v1/notifications", headers=account.headers)
    return [item for item in response.json()["items"] if item["type"] == "expenses.price_alert"]


async def test_the_same_thing_under_two_names_is_one_product_priced_per_kg(
    client: AsyncClient,
) -> None:
    """Plan, Phase 7: "1 kg tomato ₹40 @A", "500 g tomato ₹25 @B" and "thakkali 2 kg ₹70"
    resolve to one product, charted at ₹40, ₹50 and ₹35 per kg."""
    account = await register(client)
    await bought(
        client,
        account,
        title="Tomato",
        amount_minor=4000,
        quantity=1,
        unit="kg",
        merchant="A",
        date="2026-10-01",
    )
    await bought(
        client,
        account,
        title="tomato",
        amount_minor=2500,
        quantity=500,
        unit="g",
        merchant="B",
        date="2026-10-04",
    )
    await bought(
        client,
        account,
        title="Thakkali",
        amount_minor=7000,
        quantity=2,
        unit="kg",
        date="2026-10-07",
    )
    tomato, thakkali = (
        await named(client, account, "Tomato"),
        await named(client, account, "Thakkali"),
    )

    merged = await client.post(
        f"{PRODUCTS}/{tomato['id']}/merge",
        headers=account.headers,
        json={"product_id": thakkali["id"]},
    )

    assert merged.status_code == 200, merged.text
    assert merged.json()["aliases"] == ["tomato", "thakkali"]
    assert [item["name"] for item in await products(client, account)] == ["Tomato"]
    found = await detail(client, account, tomato["id"])
    assert found["price_unit"] == "kg"
    assert [item["unit_price_minor"] for item in found["purchases"]] == [4000, 5000, 3500]
    assert found["stats"] == {
        "count": 3,
        "min_minor": 3500,
        "max_minor": 5000,
        "avg_minor": 4167,
        "last_minor": 3500,
        "previous_minor": 5000,
        "change_pct": -30.0,
    }
    assert [(item["merchant"], item["avg_minor"]) for item in found["merchants"]] == [
        (None, 3500),
        ("A", 4000),
        ("B", 5000),
    ]
    assert found["cheapest"] == "A"  # the cheapest of the places that were noted
    gone = await client.get(f"{PRODUCTS}/{thakkali['id']}", headers=account.headers)
    assert gone.status_code == 404


async def test_the_list_shows_the_latest_price_its_change_and_trend(client: AsyncClient) -> None:
    account = await register(client)
    for day, amount in (("2026-10-01", 3000), ("2026-10-05", 3300)):
        await bought(
            client,
            account,
            title="Milk",
            amount_minor=amount,
            quantity=1,
            unit="l",
            merchant="Aavin",
            date=day,
        )
    await bought(
        client,
        account,
        title="Eggs",
        amount_minor=8400,
        quantity=1,
        unit="dozen",
        date="2026-10-03",
    )

    milk, eggs = await products(client, account)  # most recently bought first

    assert milk | {"id": ""} == {
        "id": "",
        "name": "Milk",
        "aliases": ["milk"],
        "dimension": "volume",
        "price_unit": "l",
        "alert_above_minor": None,
        "apart": [],
        "currency": "INR",
        "purchases": 2,
        "last": {"unit_price_minor": 3300, "date": "2026-10-05", "merchant": "Aavin"},
        "change_pct": 10.0,
        "trend": [3000, 3300],
        "cheapest": "Aavin",
    }
    assert (eggs["name"], eggs["price_unit"], eggs["last"]["unit_price_minor"]) == (
        "Eggs",
        "dozen",
        8400,
    )
    claimed = await products(client, account, "&title=MILK")
    assert [item["name"] for item in claimed] == ["Milk"]


async def test_only_what_is_bought_by_measure_is_followed_on_its_own(client: AsyncClient) -> None:
    account = await register(client)
    await bought(client, account, title="Coffee", amount_minor=2500)
    await bought(client, account, title="Coffee", amount_minor=3000)
    await add(
        client,
        account,
        kind="income",
        title="Salary",
        category_id="salary",
        amount_minor=5000000,
        quantity=1,
        unit="piece",
    )

    assert await products(client, account) == []

    tracked = await client.post(PRODUCTS, headers=account.headers, json={"title": "coffee"})

    assert tracked.status_code == 201, tracked.text
    assert tracked.json()["dimension"] == "count"  # no quantity: one of it each time
    found = await detail(client, account, tracked.json()["id"])
    assert [item["unit_price_minor"] for item in found["purchases"]] == [2500, 3000]
    again = await client.post(PRODUCTS, headers=account.headers, json={"title": "Coffee"})
    assert again.status_code == 409
    assert again.json()["code"] == "EXPENSE_PRODUCT_NAME_TAKEN"


async def test_names_belong_to_one_product(client: AsyncClient) -> None:
    account = await register(client)
    await bought(client, account, title="Rice", quantity=5, unit="kg")
    await bought(client, account, title="Ponni rice", quantity=10, unit="kg")
    rice, ponni = await named(client, account, "Rice"), await named(client, account, "Ponni rice")

    taken = await client.patch(
        f"{PRODUCTS}/{ponni['id']}",
        headers=account.headers,
        json={"aliases": ["Ponni rice", "RICE"]},
    )
    renamed = await client.patch(
        f"{PRODUCTS}/{ponni['id']}", headers=account.headers, json={"name": "rice"}
    )
    taught = await client.patch(
        f"{PRODUCTS}/{rice['id']}",
        headers=account.headers,
        json={"aliases": ["rice", "Arisi", "அரிசி", "arisi"]},
    )

    assert taken.status_code == 409
    assert taken.json()["code"] == "EXPENSE_PRODUCT_NAME_TAKEN"
    assert taken.json()["details"] == {"product_id": rice["id"], "name": "Rice"}
    assert renamed.status_code == 409
    assert renamed.json()["code"] == "EXPENSE_PRODUCT_EXISTS"
    assert taught.json()["aliases"] == ["rice", "arisi", "அரிசி"]
    await bought(client, account, title="அரிசி", amount_minor=30000, quantity=5, unit="kg")
    assert len((await detail(client, account, rice["id"]))["purchases"]) == 2


async def test_prices_can_be_shown_per_another_unit_of_the_same_kind(client: AsyncClient) -> None:
    account = await register(client)
    await bought(client, account, title="Cardamom", amount_minor=30000, quantity=100, unit="g")
    cardamom = await named(client, account, "Cardamom")
    path = f"{PRODUCTS}/{cardamom['id']}"

    per_gram = await client.patch(path, headers=account.headers, json={"price_unit": "g"})
    per_litre = await client.patch(path, headers=account.headers, json={"price_unit": "l"})
    unknown = await client.patch(path, headers=account.headers, json={"price_unit": "tola"})

    assert cardamom["price_unit"] == "kg"  # grams are priced per kg
    assert per_gram.json()["price_unit"] == "g"
    assert (await detail(client, account, cardamom["id"]))["stats"]["last_minor"] == 300
    assert per_litre.status_code == 422
    assert per_litre.json()["code"] == "EXPENSE_PRODUCT_INVALID"
    assert unknown.status_code == 422


async def test_a_price_above_the_alert_is_told_once_per_entry(client: AsyncClient) -> None:
    account = await register(client)
    await bought(client, account, title="Onion", amount_minor=4000, quantity=1, unit="kg")
    onion = await named(client, account, "Onion")
    set_alert = await client.patch(
        f"{PRODUCTS}/{onion['id']}", headers=account.headers, json={"alert_above_minor": 5000}
    )

    pricey = await bought(
        client, account, title="onion", amount_minor=12000, quantity=2, unit="kg", merchant="Market"
    )
    await bought(client, account, title="Onion", amount_minor=2500, quantity=500, unit="g")
    await save(client, account, pricey, amount_minor=13000, quantity=2, unit="kg")

    assert set_alert.json()["alert_above_minor"] == 5000
    told = await alerts(client, account)
    assert [item["params"] for item in told] == [
        {
            "module": "expenses",
            "product": "Onion",
            "product_id": onion["id"],
            "price_minor": 6000,
            "limit_minor": 5000,
            "currency": "INR",
            "unit": "kg",
        }
    ]
    switched_off = await client.patch(
        f"{PRODUCTS}/{onion['id']}", headers=account.headers, json={"alert_above_minor": None}
    )
    assert switched_off.json()["alert_above_minor"] is None


async def test_a_product_can_stop_being_followed(client: AsyncClient) -> None:
    account = await register(client)
    entry = await bought(client, account, title="Sugar", quantity=1, unit="kg")
    sugar = await named(client, account, "Sugar")

    stopped = await client.delete(f"{PRODUCTS}/{sugar['id']}", headers=account.headers)
    await bought(client, account, title="sugar", quantity=2, unit="kg")

    assert stopped.status_code == 204
    assert await products(client, account) == []  # not followed again on its own
    still = await client.get(f"{ENTRIES}/{entry['id']}", headers=account.headers)
    assert still.status_code == 200
    tracked = await client.post(PRODUCTS, headers=account.headers, json={"title": "Sugar"})
    assert tracked.status_code == 201
    assert len((await detail(client, account, tracked.json()["id"]))["purchases"]) == 2


async def test_products_said_to_differ_remember_it(client: AsyncClient) -> None:
    account = await register(client)
    await bought(client, account, title="Tomato", quantity=1, unit="kg")
    await bought(client, account, title="Tomato sauce", quantity=1, unit="bottle")
    tomato, sauce = (
        await named(client, account, "Tomato"),
        await named(client, account, "Tomato sauce"),
    )

    apart = await client.post(
        f"{PRODUCTS}/{tomato['id']}/apart",
        headers=account.headers,
        json={"product_id": sauce["id"]},
    )
    itself = await client.post(
        f"{PRODUCTS}/{tomato['id']}/merge",
        headers=account.headers,
        json={"product_id": tomato["id"]},
    )

    assert apart.status_code == 204
    assert itself.status_code == 422
    stored = {item["name"]: item for item in await products(client, account)}
    assert stored["Tomato"]["apart"] == [sauce["id"]]
    assert stored["Tomato sauce"]["apart"] == [tomato["id"]]

    await bought(client, account, title="Ketchup", quantity=1, unit="bottle")
    ketchup = await named(client, account, "Ketchup")
    await client.post(
        f"{PRODUCTS}/{ketchup['id']}/merge",
        headers=account.headers,
        json={"product_id": sauce["id"]},
    )
    # Combined, the sauce's "different from tomato" now belongs to ketchup.
    assert (await named(client, account, "Ketchup"))["apart"] == [tomato["id"]]


async def test_the_price_history_can_be_limited_to_some_days(client: AsyncClient) -> None:
    account = await register(client)
    for day in ("2026-09-01", "2026-10-01", "2026-10-08"):
        await bought(client, account, title="Dal", quantity=1, unit="kg", date=day)
    dal = await named(client, account, "Dal")

    october = await detail(client, account, dal["id"], "?from=2026-10-01&to=2026-10-07")

    assert [item["date"] for item in october["purchases"]] == ["2026-10-01"]


async def test_products_are_private(client: AsyncClient) -> None:
    owner, other = await register(client), await register(client)
    await bought(client, owner, title="Ghee", quantity=1, unit="l")
    ghee = await named(client, owner, "Ghee")

    seen = await client.get(f"{PRODUCTS}/{ghee['id']}", headers=other.headers)
    merged = await client.post(
        f"{PRODUCTS}/{ghee['id']}/merge", headers=other.headers, json={"product_id": ghee["id"]}
    )

    assert (seen.status_code, merged.status_code) == (404, 404)
    assert await products(client, other) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 1},  # no unit
        {"unit": "kg"},  # no quantity
        {"quantity": 0, "unit": "kg"},
        {"quantity": 2, "unit": "tola"},
    ],
)
async def test_a_quantity_needs_a_known_unit(client: AsyncClient, changes: dict[str, Any]) -> None:
    account = await register(client)

    response = await client.post(
        ENTRIES,
        headers=account.headers,
        json={
            "title": "Rice",
            "amount_minor": 100,
            "category_id": "groceries",
            "date": "2026-10-08",
            **changes,
        },
    )

    assert response.status_code == 422


async def test_units_are_listed_with_what_they_measure(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.get("/v1/expenses/units", headers=account.headers)

    found = {item["code"]: item for item in response.json()}
    assert found["g"] == {"code": "g", "dimension": "mass", "factor": 0.001}
    assert found["dozen"] == {"code": "dozen", "dimension": "count", "factor": 12.0}
