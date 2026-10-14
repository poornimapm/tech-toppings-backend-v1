"""Products: names that group the user's entries, and what those cost over time (ADR-0015)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from contextlib import suppress
from typing import Any, ClassVar

from pymongo.errors import DuplicateKeyError

from app.modules.expenses.domain import MODULE, EntryKind
from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.schemas import on_days
from app.modules.expenses.features.products.models import Product, ProductRepository
from app.modules.expenses.features.products.prices import (
    COUNT,
    Purchase,
    by_merchant,
    cheapest,
    purchase,
    stats,
)
from app.modules.expenses.features.products.schemas import (
    LastPrice,
    ProductDetail,
    ProductOut,
    ProductPatch,
    ProductSummary,
    TrackIn,
)
from app.modules.expenses.units import price_unit_for, units
from app.platform import ActorContext, AppError, ConflictError, Inbox, PageParams, normalise

MAX_PRODUCTS = 500
TREND_POINTS = 12
FIELDS = ("title", "date", "merchant", "quantity", "unit", "amount_minor")


def key(title: str) -> str:
    """How a title is compared: "Tomato " and "tomato" are the same name."""
    return normalise(title)


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


class ProductExistsError(ConflictError):
    code: ClassVar[str] = "EXPENSE_PRODUCT_EXISTS"
    default_message = "You already follow a product with this name."


class ProductNameTakenError(ConflictError):
    code: ClassVar[str] = "EXPENSE_PRODUCT_NAME_TAKEN"
    default_message = "Another product already goes by this name; combine them instead."


class ProductLimitError(ConflictError):
    code: ClassVar[str] = "EXPENSE_PRODUCT_LIMIT"
    default_message = "You follow as many products as you can."


class ProductInvalidError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_PRODUCT_INVALID"
    default_message = "That can't be done with these products."


class Products:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repo = ProductRepository(ctx.scope, ctx.clock)
        self.entries = EntryRepository(ctx.scope, ctx.clock)

    # ------------------------------------------------------------------ reads

    async def _titles(self) -> dict[str, list[str]]:
        """The titles of the user's expenses, by the name they compare as."""
        titles = await Entry.get_pymongo_collection().distinct(
            "title", self.entries.query({"kind": EntryKind.EXPENSE})
        )
        found: dict[str, list[str]] = {}
        for title in titles:
            found.setdefault(key(title), []).append(title)
        return found

    async def _rows(
        self,
        aliases: Iterable[str],
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> list[dict[str, Any]]:
        """Expenses whose title is one of the names, oldest first."""
        names = await self._titles()
        criteria = {
            "kind": EntryKind.EXPENSE,
            "currency": self.ctx.user.base_currency,
            "title": {"$in": [title for alias in aliases for title in names.get(alias, [])]},
        }
        if start or end:
            criteria |= on_days(start or dt.date.min, end or dt.date.max)
        cursor = Entry.get_pymongo_collection().find(
            self.entries.query(criteria), dict.fromkeys(FIELDS, 1)
        )
        return await cursor.sort([("date", 1), ("created_at", 1), ("_id", 1)]).to_list()

    async def live(self, alias: str | None = None) -> list[Product]:
        criteria = {"aliases": alias} if alias else {}
        return await Product.find(self.repo.query(criteria)).sort("name_key").to_list()

    def _summary(self, product: Product, bought: Sequence[Purchase]) -> ProductSummary:
        prices = [item.unit_price_minor for item in bought if item.unit_price_minor is not None]
        latest = next(
            (item for item in reversed(bought) if item.unit_price_minor is not None), None
        )
        figures = stats(bought)
        return ProductSummary(
            **ProductOut.of(product).model_dump(),
            currency=self.ctx.user.base_currency,
            purchases=len(prices),
            last=LastPrice(unit_price_minor=prices[-1], date=latest.date, merchant=latest.merchant)
            if latest
            else None,
            change_pct=figures.change_pct if figures else None,
            trend=prices[-TREND_POINTS:],
            cheapest=cheapest(by_merchant(bought)),
        )

    async def overview(
        self, params: PageParams, title: str | None = None
    ) -> tuple[list[ProductSummary], int]:
        """Products bought most recently first; with ``title``, the one that claims it."""
        products = await self.live(key(title) if title else None)
        owner = {alias: product for product in products for alias in product.aliases}
        bought: dict[Any, list[Purchase]] = {product.id: [] for product in products}
        for row in await self._rows(owner):
            product = owner[key(row["title"])]
            bought[product.id].append(purchase(row, product.dimension, product.price_unit))
        summaries = [self._summary(product, bought[product.id]) for product in products]
        summaries.sort(key=lambda item: item.last.date if item.last else dt.date.min, reverse=True)
        return summaries[params.skip : params.skip + params.page_size], len(summaries)

    async def detail(
        self, product_id: str, start: dt.date | None = None, end: dt.date | None = None
    ) -> ProductDetail:
        product = await self.repo.get(product_id)
        rows = await self._rows(product.aliases, start, end)
        purchases = [purchase(row, product.dimension, product.price_unit) for row in rows]
        places = by_merchant(purchases)
        return ProductDetail(
            **ProductOut.of(product).model_dump(),
            currency=self.ctx.user.base_currency,
            purchases=purchases,
            stats=stats(purchases),
            merchants=places,
            cheapest=cheapest(places),
        )

    async def unclaimed(self, limit: int) -> list[dict[str, Any]]:
        """What the user buys that no product follows yet, most often bought first."""
        claimed = {alias for product in await self.live() for alias in product.aliases}
        cursor = await Entry.get_pymongo_collection().aggregate(
            [
                {"$match": self.entries.query({"kind": EntryKind.EXPENSE})},
                {"$group": {"_id": "$title", "times": {"$sum": 1}}},
                {"$sort": {"times": -1, "_id": 1}},
            ]
        )
        found = [
            {"title": row["_id"], "times": row["times"]}
            async for row in cursor
            if key(row["_id"]) not in claimed
        ]
        return found[:limit]

    # ------------------------------------------------------------------ writes

    async def _claimed(self, aliases: Iterable[str], but: Any = None) -> None:
        """Each name belongs to one product: refuse names another product goes by."""
        criteria = {"aliases": {"$in": list(aliases)}, "_id": {"$ne": but}}
        if other := await Product.find_one(self.repo.query(criteria)):
            raise ProductNameTakenError(details={"product_id": str(other.id), "name": other.name})

    async def _saving(self, work: Any) -> Product:
        try:
            saved: Product = await work
        except DuplicateKeyError as exc:
            pattern = (exc.details or {}).get("keyPattern", {})
            raise (ProductNameTakenError if "aliases" in pattern else ProductExistsError) from exc
        return saved

    async def _create(self, name: str, alias: str, unit: str) -> Product:
        if await Product.find(self.repo.query()).count() >= MAX_PRODUCTS:
            raise ProductLimitError(details={"max": MAX_PRODUCTS})
        await self._claimed([alias])
        price_unit = price_unit_for(unit)
        fields = {
            "name": name,
            "name_key": key(name),
            "aliases": [alias],
            "dimension": units()[price_unit].dimension,
            "price_unit": price_unit,
        }
        return await self._saving(self.repo.create(fields))

    async def track(self, body: TrackIn) -> Product:
        """Follow an entry's title. Its unit comes from the latest purchase with a quantity;
        without one, it is counted (one of it each time)."""
        alias = key(body.title)
        measured = [row["unit"] for row in await self._rows([alias]) if row.get("unit")]
        counted = next(code for code, unit in units().items() if unit.dimension == COUNT)
        return await self._create(body.name or body.title, alias, (measured or [counted])[-1])

    async def update(self, product_id: str, body: ProductPatch) -> Product:
        product = await self.repo.get(product_id)
        changes: dict[str, Any] = {}
        if body.name:
            changes |= {"name": body.name, "name_key": key(body.name)}
        if body.aliases is not None:
            changes["aliases"] = _unique(key(alias) for alias in body.aliases)
            await self._claimed(changes["aliases"], but=product.id)
        if body.price_unit:
            if units()[body.price_unit].dimension != product.dimension:
                raise ProductInvalidError(details={"dimension": product.dimension})
            changes["price_unit"] = body.price_unit
        if "alert_above_minor" in body.model_fields_set:
            changes["alert_above_minor"] = body.alert_above_minor
        return await self._saving(self.repo.update(product_id, changes)) if changes else product

    async def merge(self, product_id: str, other_id: str) -> Product:
        """``other`` becomes part of this product: its names (and so its entries) join it."""
        target = await self.repo.get(product_id)
        source = await self.repo.get(other_id)
        if source.id == target.id:
            raise ProductInvalidError
        # Names first leave the other product: each may belong to one product only.
        await self.repo.update(
            other_id, {"aliases": [], "name_key": None, "merged_into": str(target.id)}
        )
        await self.repo.soft_delete(other_id)
        ids = {str(target.id), str(source.id)}
        apart = _unique(item for item in [*target.apart, *source.apart] if item not in ids)
        aliases = _unique([*target.aliases, *source.aliases])
        return await self.repo.update(product_id, {"aliases": aliases, "apart": apart})

    async def keep_apart(self, product_id: str, other_id: str) -> None:
        """The user says the two are different things: don't suggest combining them again."""
        first, second = await self.repo.get(product_id), await self.repo.get(other_id)
        if first.id == second.id:
            raise ProductInvalidError
        for one, other in ((first, second), (second, first)):
            await self.repo.update(str(one.id), {"apart": _unique([*one.apart, str(other.id)])})

    async def stop(self, product_id: str) -> None:
        """Stop following it. Its entries stay as they are; its names won't be followed again
        on their own (tracking it by hand still can)."""
        product = await self.repo.get(product_id)
        stopped = _unique([*product.stopped, *product.aliases])
        await self.repo.update(product_id, {"stopped": stopped, "aliases": [], "name_key": None})
        await self.repo.soft_delete(product_id)

    # ------------------------------------------------------------------ entries

    async def observe(self, entry: Entry) -> None:
        """After an expense is saved: start following something new bought by measure, and
        tell the user when it cost more than they asked to hear about."""
        if entry.kind is not EntryKind.EXPENSE:
            return
        alias = key(entry.title)
        product = await Product.find_one(self.repo.query({"aliases": alias}))
        if product is None and entry.unit and not await self._stopped(alias):
            with suppress(ProductExistsError, ProductNameTakenError, ProductLimitError):
                product = await self._create(entry.title, alias, entry.unit)
        if product and product.alert_above_minor:
            await self._alert(product, entry)

    async def _stopped(self, alias: str) -> bool:
        criteria = self.repo.query({"stopped": alias}, include_deleted=True)
        return await Product.find_one(criteria) is not None

    async def _alert(self, product: Product, entry: Entry) -> None:
        row = {**entry.model_dump(), "_id": entry.id}
        price = purchase(row, product.dimension, product.price_unit).unit_price_minor
        limit = product.alert_above_minor
        if price is None or limit is None or price <= limit:
            return
        await Inbox(self.ctx.scope, self.ctx.clock, self.ctx.settings).notify(
            f"{MODULE}.price_alert",
            module=MODULE,
            dedupe_key=f"{MODULE}.price_alert:{entry.id}",  # once per entry, however it's edited
            product=product.name,
            product_id=str(product.id),
            price_minor=price,
            limit_minor=limit,
            currency=entry.currency,
            unit=product.price_unit,
        )
