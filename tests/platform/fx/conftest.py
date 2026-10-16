"""A stand-in for Frankfurter (respx), so no test ever calls the real rates provider."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from app.core.config import FxSettings

FX_URL = FxSettings(_env_file=None).provider_url
LATEST_DATE = "2026-10-08"
# How much of each currency one unit of the base bought, any day.
RATES = {
    "USD": {"INR": 83.5, "EUR": 0.92},
    "EUR": {"INR": 90.0, "USD": 1.09},
    "INR": {"USD": 0.012, "EUR": 0.0111},
}


def answer(request: httpx.Request) -> httpx.Response:
    base = request.url.params.get("base", "")
    day = request.url.path.rsplit("/", 1)[-1]
    if base not in RATES:
        return httpx.Response(404, json={"message": "not found"})
    return httpx.Response(
        200,
        json={
            "amount": 1.0,
            "base": base,
            "date": LATEST_DATE if day == "latest" else day,
            "rates": RATES[base],
        },
    )


@pytest.fixture
def fx_rates() -> Iterator[respx.Route]:
    """Frankfurter answering from RATES; the route counts the calls."""
    with respx.mock(assert_all_called=False) as mock:
        yield mock.get(url__startswith=FX_URL).mock(side_effect=answer)
