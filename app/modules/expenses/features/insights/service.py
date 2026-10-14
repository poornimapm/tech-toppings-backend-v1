"""Insights: the facts of a week or month, worked out here, and a summary of them written by
the ``explain-spending`` skill (an AI, or the skill's templates without one).

Only aggregates and the user's own labels go to the skill. An AI summary is kept with the
hash of its facts and shown again until they change, so looking twice costs one AI call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import suppress
from typing import Any

from pymongo.errors import DuplicateKeyError

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.analytics.schemas import Span
from app.modules.expenses.features.analytics.service import Analytics, change_pct
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.features.entries.schemas import EntryFilters
from app.modules.expenses.features.insights.models import Insight, InsightRepository
from app.modules.expenses.features.insights.schemas import InsightOut, Period
from app.modules.expenses.features.products.service import MAX_PRODUCTS, Products
from app.platform import ActorContext, AgentMode, PageParams, RunStatus, run_skill

SKILL = "explain-spending"
TOP = 3  # of each list in the facts: enough to write two or three sentences from


def bounds(period: Period, day: dt.date) -> Span:
    """The whole week (Monday first) or month that ``day`` is in."""
    if period is Period.WEEK:
        start = day - dt.timedelta(days=day.weekday())
        return Span(start=start, end=start + dt.timedelta(days=6))
    following = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return Span(start=day.replace(day=1), end=following - dt.timedelta(days=1))


def _hash(facts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


class Insights:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.analytics = Analytics(ctx)
        self.stored = InsightRepository(ctx.scope, ctx.clock)

    async def facts(self, period: Period, day: dt.date) -> dict[str, Any]:
        """What happened in the period so far, against the same days of the period before."""
        whole, today = bounds(period, day), self.analytics.today()
        span = Span(start=whole.start, end=min(whole.end, today))
        earlier = bounds(period, whole.start - dt.timedelta(days=1))
        before = Span(
            start=earlier.start,
            end=min(earlier.end, earlier.start + (span.end - span.start)),
        )
        now, then = await self.analytics.totals(span), await self.analytics.totals(before)
        names = {
            item.id: item.name or item.id
            for item in await Categories(self.ctx.scope, self.ctx.clock).all()
        }
        current = EntryFilters.model_validate({"from": span.start, "to": span.end})
        previous = EntryFilters.model_validate({"from": before.start, "to": before.end})
        categories = (await self.analytics.by_category(current)).items[:TOP]
        earlier_by = {
            item.category_id: item.amount_minor
            for item in (await self.analytics.by_category(previous)).items
        }
        merchants = (await self.analytics.top_merchants(current)).items
        people = [item for item in (await self.analytics.by_person(current)).items if item.name]
        unusual = (await self.analytics.anomalies(current)).items[:TOP]
        return {
            "period": period.value,
            "start": whole.start.isoformat(),
            "end": whole.end.isoformat(),
            "upto": span.end.isoformat(),
            "partial": span.end < whole.end,
            "currency": self.analytics.currency,
            **now.model_dump(),
            "previous": {
                "start": before.start.isoformat(),
                "end": before.end.isoformat(),
                **then.model_dump(),
            },
            "spent_change_pct": change_pct(now.spent_minor, then.spent_minor),
            "savings_rate_pct": round(now.net_minor / now.received_minor * 100, 1)
            if now.received_minor
            else None,
            "top_categories": [
                {
                    "category": names.get(item.category_id, item.category_id),
                    "category_id": item.category_id,
                    "amount_minor": item.amount_minor,
                    "share_pct": item.share_pct,
                    "change_pct": change_pct(
                        item.amount_minor, earlier_by.get(item.category_id, 0)
                    ),
                }
                for item in categories
            ],
            "top_merchant": merchants[0].model_dump() if merchants else None,
            "people": [
                {"name": item.name, "amount_minor": item.amount_minor} for item in people[:TOP]
            ],
            "biggest": await self._biggest(span),
            "unusual": [
                item.model_dump(include={"title", "amount_minor", "usual_minor", "times_usual"})
                for item in unusual
            ],
            "forecast": await self._forecast(period, whole, span),
            "price_moves": await self._price_moves(span),
        }

    async def _biggest(self, span: Span) -> dict[str, Any] | None:
        row = await Entry.get_pymongo_collection().find_one(
            self.analytics.criteria(EntryFilters(), span, EntryKind.EXPENSE),
            {"title": 1, "amount_minor": 1, "date": 1},
            sort=[("amount_minor", -1), ("date", -1)],
        )
        return row and {
            "title": row["title"],
            "amount_minor": row["amount_minor"],
            "date": row["date"],
        }

    async def _forecast(self, period: Period, whole: Span, span: Span) -> dict[str, Any] | None:
        if period is not Period.MONTH or span.end >= whole.end:
            return None
        found = await self.analytics.forecast(whole.start)
        return found.model_dump(
            include={"projected_minor", "low_minor", "high_minor", "previous_month_minor"}
        )

    async def _price_moves(self, span: Span) -> list[dict[str, Any]]:
        """Products bought in the period whose price changed since the purchase before."""
        found, _ = await Products(self.ctx).overview(PageParams(1, MAX_PRODUCTS))
        moved = [
            item
            for item in found
            if item.change_pct and item.last and span.start <= item.last.date <= span.end
        ]
        moved.sort(key=lambda item: -abs(item.change_pct or 0))
        return [
            {
                "product": item.name,
                "change_pct": item.change_pct,
                "unit_price_minor": item.last.unit_price_minor if item.last else None,
                "price_unit": item.price_unit,
            }
            for item in moved[:TOP]
        ]

    async def explain(
        self, period: Period, day: dt.date | None, *, refresh: bool = False
    ) -> InsightOut:
        """The facts and their summary: the kept AI summary while the facts are unchanged,
        else a new one from the skill (``refresh`` asks for a new one regardless)."""
        whole = bounds(period, day or self.analytics.today())
        facts = await self.facts(period, whole.start)
        digest, locale = _hash(facts), self.ctx.user.locale
        key = {"period": period.value, "period_start": whole.start.isoformat(), "locale": locale}
        base: dict[str, Any] = {
            "period": period,
            "start": whole.start,
            "end": whole.end,
            "facts": facts,
        }
        kept = await Insight.find_one(self.stored.query(key))
        if kept and kept.facts_hash == digest and not refresh:
            return InsightOut(
                **base,
                status=RunStatus.COMPLETED,
                mode=AgentMode.AGENT,
                reason=None,
                message=kept.message,
                message_code=None,
                message_params={},
                cached=True,
            )

        run = await run_skill(
            self.ctx,
            SKILL,
            f"{period.value} {whole.start.isoformat()}",
            locale=locale,
            context={"period": period.value, "date": whole.start.isoformat()},
        )
        if run.mode is AgentMode.AGENT and run.status is RunStatus.COMPLETED and run.message:
            fields: dict[str, Any] = {
                "facts_hash": digest,
                "facts": facts,
                "message": run.message,
                "provider": run.provider,
                "generated_at": self.ctx.clock.now(),
            }
            await self._keep(kept, key, fields)
        return InsightOut(
            **base,
            status=run.status,
            mode=run.mode,
            reason=run.reason,
            message=run.message,
            message_code=run.message_code,
            message_params=run.message_params,
            cached=False,
        )

    async def _keep(
        self, kept: Insight | None, key: dict[str, str], fields: dict[str, Any]
    ) -> None:
        if kept is not None and kept.id is not None:
            await self.stored.update(kept.id, fields)
            return
        with suppress(DuplicateKeyError):  # another tab wrote it first: theirs will do
            await self.stored.create({**key, **fields})
