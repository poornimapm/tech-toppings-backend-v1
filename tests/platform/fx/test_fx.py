"""``GET /v1/fx/rates``: a currency's rates on a day, fetched once and kept in MongoDB (today's
again after FX_CACHE_TTL_HOURS), the kept ones used when the provider can't be reached."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

from app.platform.fx.models import FxRate
from tests.helpers import FrozenClock, register
from tests.platform.fx.conftest import FX_URL

RATES = "/v1/fx/rates"


async def forget_rates() -> None:
    """Rates are shared by every user and test: each test here starts with none kept."""
    await FxRate.get_pymongo_collection().delete_many({})


async def test_a_day_s_rates_are_fetched_once(client: AsyncClient, fx_rates: respx.Route) -> None:
    account = await register(client)
    await forget_rates()
    first = await client.get(
        RATES, headers=account.headers, params={"base": "USD", "date": "2026-09-05"}
    )
    again = await client.get(
        RATES, headers=account.headers, params={"base": "USD", "date": "2026-09-05"}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "base": "USD",
        "date": "2026-09-05",
        "rates": {"INR": "83.5", "EUR": "0.92"},
    }
    assert again.json() == first.json()
    assert fx_rates.call_count == 1  # a past day's rates never change


async def test_my_base_currency_by_default_and_today_s_rates_again_after_a_while(
    client: AsyncClient, fx_rates: respx.Route, clock: FrozenClock
) -> None:
    account = await register(client)  # in rupees
    await forget_rates()
    today = await client.get(RATES, headers=account.headers)
    assert today.json()["base"] == "INR"
    await client.get(RATES, headers=account.headers)
    assert fx_rates.call_count == 1
    clock.advance(hours=13)  # FX_CACHE_TTL_HOURS is 12
    await client.get(RATES, headers=account.headers)
    assert fx_rates.call_count == 2


async def test_kept_rates_are_used_when_the_provider_is_away(
    client: AsyncClient, fx_rates: respx.Route, clock: FrozenClock
) -> None:
    account = await register(client)
    await forget_rates()
    kept = await client.get(RATES, headers=account.headers, params={"base": "EUR"})
    clock.advance(hours=13)
    fx_rates.side_effect = httpx.ConnectError("down")
    stale = await client.get(RATES, headers=account.headers, params={"base": "EUR"})
    assert stale.status_code == 200
    assert stale.json() == kept.json()

    # Rates are shared by every user: one no test has fetched.
    never = await client.get(RATES, headers=account.headers, params={"base": "JPY"})
    assert (never.status_code, never.json()["code"]) == (503, "FX_UNAVAILABLE")


async def test_a_currency_the_provider_does_not_know(client: AsyncClient) -> None:
    account = await register(client)
    await forget_rates()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=FX_URL).mock(return_value=httpx.Response(404, json={}))
        response = await client.get(RATES, headers=account.headers, params={"base": "XYZ"})
    assert (response.status_code, response.json()["code"]) == (422, "FX_CURRENCY_UNSUPPORTED")


async def test_needs_a_signed_in_user(client: AsyncClient) -> None:
    assert (await client.get(RATES)).status_code == 401
