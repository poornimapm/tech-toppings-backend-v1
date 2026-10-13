"""Categories: the system ones from data/categories.yaml plus the user's own."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Annotated, Any, ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints
from pymongo.errors import DuplicateKeyError

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.categories.models import Category, CategoryRepository
from app.platform import AppError, Clock, ConflictError, PageParams, Scope

DATA = Path(__file__).parents[2] / "data" / "categories.yaml"
MAX_CUSTOM = 100

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Icon = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)]
Accent = Annotated[str, StringConstraints(pattern=r"^[a-z]{3,12}$")]


class CategoryInvalidError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_CATEGORY_INVALID"
    default_message = "Choose one of your categories for this kind of entry."


class CategoryExistsError(ConflictError):
    code: ClassVar[str] = "EXPENSE_CATEGORY_EXISTS"
    default_message = "You already have a category with this name."


class CategoryLimitError(ConflictError):
    code: ClassVar[str] = "EXPENSE_CATEGORY_LIMIT"
    default_message = "You have reached the limit of your own categories."


class CategoryOut(BaseModel):
    """``key`` (system) is translated by the client; ``name`` (custom) is the user's text."""

    id: str
    kind: EntryKind
    key: str | None = None
    name: str | None = None
    icon: str
    accent: str
    system: bool = False
    archived: bool = False

    @classmethod
    def of(cls, category: Category) -> CategoryOut:
        return cls(
            id=str(category.id),
            kind=category.kind,
            name=category.name,
            icon=category.icon,
            accent=category.accent,
            archived=category.deleted_at is not None,
        )


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntryKind = EntryKind.EXPENSE
    name: Name
    icon: Icon
    accent: Accent


class CategoryPatch(BaseModel):
    """The kind is fixed: entries already filed under the category rely on it."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    icon: Icon | None = None
    accent: Accent | None = None


@cache
def system_categories() -> tuple[CategoryOut, ...]:
    data: dict[str, list[dict[str, Any]]] = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    return tuple(
        CategoryOut(id=item["key"], kind=EntryKind(kind), system=True, **item)
        for kind, items in data.items()
        for item in items
    )


class Categories:
    def __init__(self, scope: Scope, clock: Clock) -> None:
        self.custom = CategoryRepository(scope, clock)

    async def all(self) -> list[CategoryOut]:
        """System first, then the user's own by name; archived ones last (still listed, so
        entries filed under them keep their name)."""
        mine, _ = await self.custom.list(
            PageParams(1, 2 * MAX_CUSTOM),
            [("deleted_at", 1), ("name_key", 1)],
            include_deleted=True,
        )
        return [*system_categories(), *(CategoryOut.of(category) for category in mine)]

    async def ensure_usable(self, category_id: str, kind: EntryKind) -> None:
        """A new or re-filed entry needs a live category of its own kind."""
        if any(item.id == category_id and item.kind is kind for item in system_categories()):
            return
        found = await self.custom.find(category_id)
        if found is None or found.kind is not kind:
            raise CategoryInvalidError(details={"category_id": category_id})

    async def create(self, body: CategoryIn) -> Category:
        if await Category.find(self.custom.query()).count() >= MAX_CUSTOM:
            raise CategoryLimitError(details={"max": MAX_CUSTOM})
        return await self._saving(
            self.custom.create({**body.model_dump(), "name_key": body.name.casefold()})
        )

    async def update(self, category_id: str, body: CategoryPatch) -> Category:
        changes = body.model_dump(exclude_none=True)
        if body.name:
            changes["name_key"] = body.name.casefold()
        return await self._saving(self.custom.update(category_id, changes))

    async def archive(self, category_id: str) -> None:
        await self.custom.update(category_id, {"name_key": None})
        await self.custom.soft_delete(category_id)

    @staticmethod
    async def _saving(work: Any) -> Category:
        try:
            saved: Category = await work
        except DuplicateKeyError as exc:
            raise CategoryExistsError from exc
        return saved
