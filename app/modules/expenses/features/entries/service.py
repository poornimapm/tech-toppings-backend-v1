"""Entries: the rules around the revisioned repository (currency, category, activity)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, ClassVar

from app.modules.expenses.domain import MODULE, EntrySource
from app.modules.expenses.features.budgets.service import Budgets
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry, EntryRepository, sums
from app.modules.expenses.features.entries.schemas import (
    CurrencyTotal,
    EntryFilters,
    EntryIn,
    FacetsOut,
    SummaryOut,
)
from app.modules.expenses.features.products.service import Products
from app.platform import ActivityLog, ActorContext, AppError

MAX_SUGGESTIONS = 50


class CurrencyUnsupportedError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_CURRENCY_UNSUPPORTED"
    default_message = "For now, entries are kept in your base currency."


def _suggestions(values: Iterable[Any]) -> list[str]:
    return sorted(value for value in values if value)[:MAX_SUGGESTIONS]


class EntryService:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.entries = EntryRepository(ctx.scope, ctx.clock)
        self.categories = Categories(ctx.scope, ctx.clock)
        self.products = Products(ctx)
        self.budgets = Budgets(ctx)
        self.activity = ActivityLog(ctx.scope, ctx.clock, ctx.settings)

    async def _stored(
        self, body: EntryIn, current: Entry | None = None, *, kept: bool = False
    ) -> dict[str, Any]:
        """What to store for ``body``, once its currency and category are known to be usable.
        An entry keeps the currency it was made in; a new one uses the user's base currency
        (other currencies arrive with exchange rates). ``kept``: its category was checked
        already and is kept even archived (a repeating rule's)."""
        currency = current.currency if current else self.ctx.user.base_currency
        if body.currency not in (None, currency):
            raise CurrencyUnsupportedError(details={"currency": currency})
        refiled = current is None or (body.category_id, body.kind) != (
            current.category_id,
            current.kind,
        )
        if refiled and not kept:
            await self.categories.ensure_usable(body.category_id, body.kind)
        fields = body.model_dump(mode="json", exclude={"revision_no"})
        return {**fields, "currency": currency}

    async def _changed(self, event: str, before: Entry | None, after: Entry) -> Entry:
        """Note the change in the user's activity, unless nothing actually changed."""
        if before is None or before.revision_no != after.revision_no:
            await self.activity.record(f"{MODULE}.entry_{event}", module=MODULE, title=after.title)
        return after

    async def _counted(self, entry: Entry) -> None:
        """An entry now counts as spent (new, edited or back): follow its price, and warn about
        budgets it brings near or over their limit."""
        await self.products.observe(entry)
        await self.budgets.observe(entry)

    # ------------------------------------------------------------------ writes

    async def create(
        self,
        body: EntryIn,
        client_key: str | None = None,
        source: EntrySource = EntrySource.MANUAL,
        recurring_id: str | None = None,
    ) -> tuple[Entry, bool]:
        """The new entry, or the one an earlier request with this key made (then ``False``).
        ``recurring_id``: the repeating rule it comes from, whose category it is filed under
        even once archived, as the rule keeps it."""
        stored = await self._stored(body, kept=recurring_id is not None)
        fields = {**stored, "source": source, "recurring_id": recurring_id}
        entry, created = await self.entries.create_once(fields, client_key, source=source)
        if not created:
            return entry, False
        await self._counted(entry)
        return await self._changed("added", None, entry), True

    async def update(self, entry_id: str, body: EntryIn, expected: int) -> Entry:
        current = await self.entries.get(entry_id)
        fields = await self._stored(body, current)
        entry = await self.entries.update(
            entry_id, fields, expected=expected, source=EntrySource.MANUAL
        )
        if entry.revision_no != current.revision_no:
            await self._counted(entry)
        return await self._changed("updated", current, entry)

    async def delete(self, entry_id: str) -> None:
        current = await self.entries.get(entry_id)
        await self.entries.soft_delete(entry_id, source=EntrySource.MANUAL)
        await self._changed("deleted", None, current)

    async def undelete(self, entry_id: str) -> Entry:
        current = await self.entries.get(entry_id, include_deleted=True)
        entry = await self.entries.undelete(entry_id, source=EntrySource.MANUAL)
        if entry.revision_no != current.revision_no:
            await self.budgets.observe(entry)
        return await self._changed("undeleted", current, entry)

    async def restore(self, entry_id: str, revision_no: int) -> Entry:
        current = await self.entries.get(entry_id)
        entry = await self.entries.restore(entry_id, revision_no, source=EntrySource.MANUAL)
        if entry.revision_no != current.revision_no:
            await self.budgets.observe(entry)
        return await self._changed("restored", current, entry)

    # ------------------------------------------------------------------ reads

    async def summary(self, filters: EntryFilters) -> SummaryOut:
        criteria = self.entries.query(filters.criteria(), include_deleted=filters.deleted)
        totals: dict[str, CurrencyTotal] = {}
        for row in await sums(criteria, "currency", "kind"):
            total = totals.setdefault(row["currency"], CurrencyTotal(currency=row["currency"]))
            setattr(total, f"{row['kind']}_minor", row["amount_minor"])
            total.count += row["count"]
        return SummaryOut(totals=sorted(totals.values(), key=lambda total: total.currency))

    async def facets(self) -> FacetsOut:
        collection, mine = Entry.get_pymongo_collection(), self.entries.query()
        tags, people, merchants = await asyncio.gather(
            *(collection.distinct(name, mine) for name in ("tags", "for_whom", "merchant"))
        )
        return FacetsOut(
            tags=_suggestions(tags),
            for_whom=_suggestions(people),
            merchants=_suggestions(merchants),
        )
