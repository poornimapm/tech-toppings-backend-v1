from __future__ import annotations

from typing import Any, ClassVar

from pymongo import ASCENDING, IndexModel

from app.modules.expenses.domain import MODULE
from app.platform import OwnedDocument, ScopedRepository


class SavedFilter(OwnedDocument):
    """A named view of the entries list. A deleted one keeps its record but gives up
    ``name_key``, so the name can be used again."""

    MODULE_KEY: ClassVar[str] = MODULE

    name: str
    name_key: str | None  # the name as compared: one view per name
    view: dict[str, Any]  # a SavedView, as stored

    class Settings:
        name = "expense_saved_filters"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel(
                [("user_id", ASCENDING), ("name_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"name_key": {"$type": "string"}},
                name="user_name_unique",
            )
        ]


class SavedFilterRepository(ScopedRepository[SavedFilter]):
    model: ClassVar[type[OwnedDocument]] = SavedFilter
