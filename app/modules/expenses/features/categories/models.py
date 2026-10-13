from __future__ import annotations

from typing import ClassVar

from pymongo import ASCENDING, IndexModel

from app.modules.expenses.domain import MODULE, EntryKind
from app.platform import OwnedDocument, ScopedRepository


class Category(OwnedDocument):
    """A category the user added. System categories are data (data/categories.yaml), not
    records. An archived one keeps its record (``deleted_at``) so old entries still show its
    name, but gives up ``name_key`` so the name can be used again."""

    MODULE_KEY: ClassVar[str] = MODULE

    kind: EntryKind
    name: str
    name_key: str | None  # the name lower-cased: one category per name and kind
    icon: str
    accent: str

    class Settings:
        name = "expense_categories"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel(
                [("user_id", ASCENDING), ("kind", ASCENDING), ("name_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"name_key": {"$type": "string"}},
                name="user_kind_name_unique",
            )
        ]


class CategoryRepository(ScopedRepository[Category]):
    model: ClassVar[type[OwnedDocument]] = Category
