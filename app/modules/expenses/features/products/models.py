from __future__ import annotations

from typing import ClassVar

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.modules.expenses.domain import MODULE
from app.platform import OwnedDocument, ScopedRepository


class Product(OwnedDocument):
    """Something the user buys again and again, known by one or more names (ADR-0015).

    An entry belongs to the product whose ``aliases`` hold its title (normalised). Nothing on
    the entry points here, so combining two products or teaching one a new name never touches
    entries or their history. Stopping or merging a product gives up its names (``aliases``
    empties) so they can be claimed again, but keeps the record."""

    MODULE_KEY: ClassVar[str] = MODULE

    name: str
    name_key: str | None  # the name normalised: one product per name
    aliases: list[str]  # normalised titles; each belongs to one live product
    dimension: str  # mass, volume or count (data/units.yaml)
    price_unit: str  # prices are shown per one of this unit
    alert_above_minor: int | None = None  # per price_unit
    apart: list[str] = Field(default_factory=list)  # products the user said are different
    stopped: list[str] = Field(default_factory=list)  # names not to start tracking again
    merged_into: str | None = None

    class Settings:
        name = "expense_products"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel(
                [("user_id", ASCENDING), ("name_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"name_key": {"$type": "string"}},
                name="user_name_unique",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("aliases", ASCENDING)],
                unique=True,
                partialFilterExpression={"aliases": {"$type": "string"}},
                name="user_alias_unique",
            ),
            IndexModel([("user_id", ASCENDING), ("stopped", ASCENDING)], name="user_stopped"),
        ]


class ProductRepository(ScopedRepository[Product]):
    model: ClassVar[type[OwnedDocument]] = Product
