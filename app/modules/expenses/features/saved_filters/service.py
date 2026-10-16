"""Saved views of the entries list: a name and the list's filters, nothing more. Opening one is
the client's work (the filters go back into the list's URL), so a view never goes stale."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, ClassVar

from pymongo.errors import DuplicateKeyError

from app.modules.expenses.features.saved_filters.models import (
    SavedFilter,
    SavedFilterRepository,
)
from app.modules.expenses.features.saved_filters.schemas import (
    SavedFilterIn,
    SavedFilterOut,
    SavedFilterPatch,
    SavedView,
)
from app.modules.expenses.listing import listing
from app.platform import ActorContext, ConflictError, PageParams, normalise


class SavedFilterExistsError(ConflictError):
    code: ClassVar[str] = "EXPENSE_SAVED_FILTER_EXISTS"
    default_message = "You already have a view with this name."


class SavedFilterLimitError(ConflictError):
    code: ClassVar[str] = "EXPENSE_SAVED_FILTER_LIMIT"
    default_message = "You have as many views as you can keep. Delete one you no longer use."


def _stored(view: SavedView) -> dict[str, Any]:
    return view.model_dump(mode="json")


class SavedFilters:
    def __init__(self, ctx: ActorContext) -> None:
        self.repo = SavedFilterRepository(ctx.scope, ctx.clock)

    async def all(self) -> list[SavedFilterOut]:
        """The user's views by name."""
        most = listing().saved_filters.max_per_user
        found, _ = await self.repo.list(PageParams(1, most), [("name_key", 1)])
        return [SavedFilterOut.of(saved) for saved in found]

    async def create(self, body: SavedFilterIn) -> SavedFilterOut:
        most = listing().saved_filters.max_per_user
        if await SavedFilter.find(self.repo.query()).count() >= most:
            raise SavedFilterLimitError(details={"max": most})
        fields = {"name": body.name, "name_key": normalise(body.name), "view": _stored(body.view)}
        return SavedFilterOut.of(await self._saving(self.repo.create(fields)))

    async def update(self, saved_id: str, body: SavedFilterPatch) -> SavedFilterOut:
        changes: dict[str, Any] = {}
        if body.name is not None:
            changes |= {"name": body.name, "name_key": normalise(body.name)}
        if body.view is not None:
            changes["view"] = _stored(body.view)
        if not changes:
            return SavedFilterOut.of(await self.repo.get(saved_id))
        return SavedFilterOut.of(await self._saving(self.repo.update(saved_id, changes)))

    async def delete(self, saved_id: str) -> None:
        await self.repo.get(saved_id)  # 404 before anything changes
        await self.repo.update(saved_id, {"name_key": None})
        await self.repo.soft_delete(saved_id)

    @staticmethod
    async def _saving(work: Awaitable[SavedFilter]) -> SavedFilter:
        try:
            return await work
        except DuplicateKeyError as exc:
            raise SavedFilterExistsError from exc
