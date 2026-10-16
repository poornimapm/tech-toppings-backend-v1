"""The monthly report (D5): the month's figures from the overview's own analytics, for a page
the browser prints to PDF, and the same figures as a CSV file."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.analytics.service import Analytics, change_pct, tuning
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import EntryRepository
from app.modules.expenses.features.entries.schemas import EntryFilters, EntryOut
from app.modules.expenses.features.exports.service import safe_text
from app.modules.expenses.features.reports.schemas import MonthlyReportOut, ReportCategory
from app.platform import ActorContext, PageParams, major_text

CSV_COLUMNS = ("section", "name", "amount", "share_pct", "previous", "change_pct")


def _month_days(month: str, today: dt.date) -> tuple[dt.date, dt.date]:
    """The month's first day, and its last, or today while it is under way."""
    start = dt.date.fromisoformat(f"{month}-01")
    last = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return start, (today if start <= today <= last else last)


class Reports:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.analytics = Analytics(ctx)

    def month_of(self, month: str | None) -> str:
        return month or self.ctx.today().strftime("%Y-%m")

    async def monthly(self, month: str | None) -> MonthlyReportOut:
        month = self.month_of(month)
        start, end = _month_days(month, self.ctx.today())
        spent = EntryFilters.model_validate({"from": start, "to": end, "kind": EntryKind.EXPENSE})
        received = spent.model_copy(update={"kind": EntryKind.INCOME})
        before = self.analytics.previous(self.analytics.span(spent))
        earlier = spent.model_copy(update={"start": before.start, "end": before.end})
        # Two groups: gather keeps each result's type for at most six.
        summary, categories, previous, income = await asyncio.gather(
            self.analytics.summary(spent.model_copy(update={"kind": None})),
            self.analytics.by_category(spent),
            self.analytics.by_category(earlier),
            self.analytics.by_category(received),
        )
        merchants, people, days, largest = await asyncio.gather(
            self.analytics.top_merchants(spent),
            self.analytics.by_person(spent),
            self.analytics.daily(spent),
            self._largest(spent),
        )
        was = {item.category_id: item.amount_minor for item in previous.items}
        return MonthlyReportOut(
            month=month,
            summary=summary,
            categories=[
                ReportCategory(
                    **item.model_dump(),
                    previous_minor=was.get(item.category_id, 0),
                    change_pct=change_pct(item.amount_minor, was.get(item.category_id, 0)),
                )
                for item in categories.items
            ],
            income=income.items,
            merchants=merchants.items,
            people=people.items,
            days=days.points,
            largest=largest,
        )

    async def _largest(self, filters: EntryFilters) -> list[EntryOut]:
        entries = EntryRepository(self.ctx.scope, self.ctx.clock)
        found, _ = await entries.list(
            PageParams(1, tuning().top.limit),
            [("amount_minor", -1), ("date", -1)],
            filters.criteria(),
        )
        return [EntryOut.of(entry) for entry in found]

    async def csv_text(self, month: str | None) -> str:
        """The report's figures, a row each: totals, categories (with the month before),
        income, places and people. Amounts in the main unit; a byte-order mark first."""
        report = await self.monthly(month)
        currency = report.summary.currency
        names = {
            item.id: item.name or item.key or item.id
            for item in await Categories(self.ctx.scope, self.ctx.clock).all()
        }

        def money(amount: int | None) -> str:
            return "" if amount is None else major_text(amount, currency)

        def pct(value: float | None) -> str:
            return "" if value is None else f"{value:g}"

        summary, before = report.summary, report.summary.previous_totals
        rows: list[list[str]] = [
            ["total", "spent", money(summary.spent_minor), "", money(before.spent_minor),
             pct(summary.spent_change_pct)],
            ["total", "received", money(summary.received_minor), "",
             money(before.received_minor), pct(summary.received_change_pct)],
            ["total", "kept", money(summary.net_minor), pct(summary.savings_rate_pct),
             money(before.net_minor), ""],
        ]  # fmt: skip
        rows += [
            ["category", safe_text(names.get(item.category_id, item.category_id)),
             money(item.amount_minor), pct(item.share_pct), money(item.previous_minor),
             pct(item.change_pct)]
            for item in report.categories
        ]  # fmt: skip
        rows += [
            ["income", safe_text(names.get(item.category_id, item.category_id)),
             money(item.amount_minor), pct(item.share_pct), "", ""]
            for item in report.income
        ]  # fmt: skip
        rows += [
            ["place", safe_text(item.merchant), money(item.amount_minor), pct(item.share_pct),
             "", ""]
            for item in report.merchants
        ]  # fmt: skip
        rows += [
            ["person", safe_text(item.name), money(item.amount_minor), pct(item.share_pct), "", ""]
            for item in report.people
        ]  # fmt: skip
        buffer = io.StringIO()
        buffer.write("﻿")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
        return buffer.getvalue()
