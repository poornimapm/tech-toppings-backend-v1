from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from app.core.errors import AppError
from app.core.logging import get_logger
from app.platform.context import ActorContext
from app.platform.fx.models import FxRate

logger = get_logger("app.fx")


class FxUnavailableError(AppError):
    status_code = 503
    code: ClassVar[str] = "FX_UNAVAILABLE"
    default_message = "Exchange rates can't be reached right now. Try again shortly."


class FxUnsupportedError(AppError):
    status_code = 422
    code: ClassVar[str] = "FX_CURRENCY_UNSUPPORTED"
    default_message = "There is no exchange rate for this currency."


class Fx:
    """Rates between currencies on a day. A past day's rates never change, so they are fetched
    once; today's (and a day ahead's) are fetched again after the cache's time. When the
    provider can't be reached, rates already kept are used, however old, rather than none."""

    def __init__(self, ctx: ActorContext) -> None:
        self.settings = ctx.settings.fx
        self.http: httpx.AsyncClient = ctx.state.http
        self.clock = ctx.clock
        self.today = ctx.today()

    async def rate(self, source: str, target: str, day: dt.date) -> Decimal:
        """How much ``target`` one ``source`` bought on ``day``."""
        if source == target:
            return Decimal(1)
        rates = (await self.rates(source, day)).rates
        if target not in rates:
            raise FxUnsupportedError(details={"currency": target})
        return Decimal(rates[target])

    async def rates(self, base: str, day: dt.date | None = None) -> FxRate:
        """``base``'s rates for ``day`` (default today)."""
        asked = min(day or self.today, self.today).isoformat()
        kept = await FxRate.find_one({"base": base, "day": asked})
        stale_after = self.clock.now() - dt.timedelta(hours=self.settings.cache_ttl_hours)
        if kept and (asked < self.today.isoformat() or _aware(kept.fetched_at) > stale_after):
            return kept
        try:
            data = await self._fetch(base, "latest" if asked == self.today.isoformat() else asked)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("fx_unavailable", base=base, day=asked, error_type=type(exc).__name__)
            # The day's own, else the latest kept before it: an old rate beats none.
            earlier = (
                kept
                or await FxRate.find({"base": base, "day": {"$lte": asked}})
                .sort("-day")
                .first_or_none()
            )
            if earlier:
                return earlier
            raise FxUnavailableError from exc
        fields = {
            "rate_date": str(data["date"]),
            "rates": {name: str(value) for name, value in data["rates"].items()},
            "fetched_at": self.clock.now(),
        }
        await FxRate.get_pymongo_collection().update_one(
            {"base": base, "day": asked}, {"$set": fields}, upsert=True
        )
        return FxRate(base=base, day=asked, **fields)

    async def _fetch(self, base: str, day: str) -> dict[str, Any]:
        response = await self.http.get(
            f"{self.settings.provider_url.rstrip('/')}/{day}",
            params={"base": base},
            timeout=self.settings.timeout_seconds,
        )
        if response.status_code == httpx.codes.NOT_FOUND:  # no such currency
            raise FxUnsupportedError(details={"currency": base})
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if not isinstance(data.get("rates"), dict) or not isinstance(data.get("date"), str):
            raise TypeError("no rates in the answer")
        return data


def _aware(moment: dt.datetime) -> dt.datetime:
    """MongoDB gives datetimes back without a time zone; they are UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
