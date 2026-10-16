"""One change to many entries at once (selected on the list). Each entry is changed by the same
rules as one at a time (its revision, budgets, prices), so a bulk change is never a back door;
what can't be changed is skipped with a reason, and the rest still go through. The activity
feed notes the change once."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.modules.expenses.domain import MODULE, EntryKind, EntrySource
from app.modules.expenses.features.categories.service import CategoryInvalidError
from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.features.entries.schemas import (
    BulkAction,
    BulkIn,
    BulkOut,
    BulkSkip,
    EntryIn,
    SkipReason,
)
from app.modules.expenses.features.entries.service import EntryService
from app.platform import ActorContext, VersionConflictError

# The activity a change of many notes; one entry is noted as an edit of that entry.
NOTED = {
    BulkAction.DELETE: ("entries_deleted", "deleted"),
    BulkAction.UNDELETE: ("entries_undeleted", "undeleted"),
}
CHANGED = ("entries_changed", "updated")


def _edited(entry: Entry, body: BulkIn) -> EntryIn | None:
    """The entry with the change made, or None when it already is so."""
    fields: dict[str, Any] = entry.model_dump(include=set(EntryIn.model_fields))
    tags: list[str] = fields["tags"]
    if body.action is BulkAction.CATEGORY:
        if entry.category_id == body.category_id:
            return None
        fields["category_id"] = body.category_id
    elif body.action is BulkAction.ADD_TAG:
        if body.tag in tags:
            return None
        fields["tags"] = [*tags, body.tag]
    elif body.action is BulkAction.REMOVE_TAG:
        if body.tag not in tags:
            return None
        fields["tags"] = [tag for tag in tags if tag != body.tag]
    return EntryIn.model_validate(fields)


class Bulk:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.service = EntryService(ctx)

    async def apply(self, body: BulkIn) -> BulkOut:
        if body.category_id is not None:
            await self._known(body.category_id)
        changed: list[Entry] = []
        skipped: list[BulkSkip] = []
        for entry_id in body.ids:
            try:
                entry = await self._one(entry_id, body)
            except _SkippedError as skip:
                skipped.append(BulkSkip(id=entry_id, reason=skip.reason))
            else:
                changed.append(entry)
        await self._note(body.action, changed)
        return BulkOut(changed=[str(entry.id) for entry in changed], skipped=skipped)

    async def _one(self, entry_id: str, body: BulkIn) -> Entry:
        bringing_back = body.action is BulkAction.UNDELETE
        entry = await self.service.entries.find(entry_id, include_deleted=bringing_back)
        if entry is None:
            raise _SkippedError(SkipReason.NOT_FOUND)
        if bringing_back and entry.deleted_at is None:
            raise _SkippedError(SkipReason.UNCHANGED)
        quiet: dict[str, Any] = {"source": EntrySource.BULK, "noted": False}
        try:
            if body.action is BulkAction.DELETE:
                await self.service.delete(entry_id, **quiet)
                return entry
            if bringing_back:
                return await self.service.undelete(entry_id, **quiet)
            try:
                edited = _edited(entry, body)
            except ValidationError as exc:
                raise _SkippedError(SkipReason.TAGS_FULL) from exc
            if edited is None:
                raise _SkippedError(SkipReason.UNCHANGED)
            return await self.service.update(entry_id, edited, entry.revision_no, **quiet)
        except VersionConflictError as exc:
            raise _SkippedError(SkipReason.CONFLICT) from exc  # changed elsewhere meanwhile
        except CategoryInvalidError as exc:
            raise _SkippedError(SkipReason.KIND) from exc  # the category is for the other kind

    async def _known(self, category_id: str) -> None:
        """The category is the user's (for one kind or the other), or nothing is changed."""
        for kind in EntryKind:
            try:
                await self.service.categories.ensure_usable(category_id, kind)
            except CategoryInvalidError:
                continue
            return
        raise CategoryInvalidError(details={"category_id": category_id})

    async def _note(self, action: BulkAction, changed: list[Entry]) -> None:
        if not changed:
            return
        many, one = NOTED.get(action, CHANGED)
        if len(changed) == 1:
            title = changed[0].title
            await self.service.activity.record(f"{MODULE}.entry_{one}", module=MODULE, title=title)
        else:
            await self.service.activity.record(
                f"{MODULE}.{many}", module=MODULE, count=len(changed)
            )


class _SkippedError(Exception):
    def __init__(self, reason: SkipReason) -> None:
        super().__init__(reason)
        self.reason = reason
