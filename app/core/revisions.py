"""Append-only history for user-owned records, written without transactions (ADR-0002).

A change takes three steps:

1. one conditional update applies the change, bumps ``revision_no`` and parks the revision
   inside the record (``pending_revision``);
2. the revision is inserted (a unique index makes repeating this harmless);
3. the parked copy is cleared.

If the process dies after step 1, the next read or write of that record finishes steps 2 and
3 first, so every change has exactly one revision and none is ever lost.

A record is also created at most once per ``client_key`` (the ``Idempotency-Key`` a client
sends), so a retried or replayed request returns the record the first one made.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, TypeVar

from beanie import Document, PydanticObjectId
from beanie.odm.utils.encoder import Encoder
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

from app.core.documents import OwnedDocument
from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import PageParams
from app.core.repository import ScopedRepository

# How a change was made when the caller doesn't say (modules pass "manual", "assistant" ...).
DEFAULT_SOURCE = "api"


class VersionConflictError(ConflictError):
    code: ClassVar[str] = "VERSION_CONFLICT"
    default_message = "This was changed somewhere else. Reload it and try again."


class RevisionAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    UNDELETE = "undelete"
    RESTORE = "restore"


class FieldChange(BaseModel):
    field: str
    old: Any = None
    new: Any = None


class RevisionEntry(BaseModel):
    """One change: who made it, when and how, what changed, and the record's fields after it."""

    revision_no: int
    action: RevisionAction
    changed_at: datetime
    changed_by: PydanticObjectId
    source: str
    changes: list[FieldChange]
    snapshot: dict[str, Any]


class Revision(Document, RevisionEntry):
    """Base for a module's revisions collection. Rows are only ever inserted."""

    entity_id: PydanticObjectId
    user_id: PydanticObjectId


REVISION_INDEXES = [
    IndexModel(
        [("entity_id", ASCENDING), ("revision_no", DESCENDING)],
        unique=True,
        name="entity_revision_unique",
    ),
    IndexModel([("user_id", ASCENDING), ("changed_at", DESCENDING)], name="user_recent"),
]


class RevisionedDocument(OwnedDocument):
    """A record with history. Its own fields must be JSON values (text, numbers, lists ...),
    so a snapshot is exactly what is stored and can be put back as it is."""

    revision_no: int = 1
    pending_revision: RevisionEntry | None = None
    client_key: str | None = None


# One record per (user, client key): what makes creating idempotent.
CLIENT_KEY_INDEX = IndexModel(
    [("user_id", ASCENDING), ("client_key", ASCENDING)],
    unique=True,
    partialFilterExpression={"client_key": {"$type": "string"}},
    name="client_key_unique",
)

TRev = TypeVar("TRev", bound=RevisionedDocument)


class RevisionedRepository(ScopedRepository[TRev]):
    """A ``ScopedRepository`` whose every write also writes one revision."""

    revisions: ClassVar[type[Revision]]

    def _fields(self, document: RevisionedDocument) -> dict[str, Any]:
        """The record's own fields (no ownership, audit or bookkeeping) as stored."""
        own = set(type(document).model_fields) - set(RevisionedDocument.model_fields)
        fields: dict[str, Any] = document.model_dump(mode="json", include=own)
        return fields

    def _entry(
        self,
        number: int,
        action: RevisionAction,
        changes: Sequence[FieldChange],
        snapshot: dict[str, Any],
        source: str,
    ) -> RevisionEntry:
        return RevisionEntry(
            revision_no=number,
            action=action,
            changed_at=self.clock.now(),
            changed_by=self.scope.actor_id,
            source=source,
            changes=list(changes),
            snapshot=snapshot,
        )

    async def _complete(self, document: TRev) -> TRev:
        """Steps 2 and 3. Safe to run any number of times, from any request."""
        pending = document.pending_revision
        if pending is None or document.id is None:
            return document
        with suppress(DuplicateKeyError):
            await self.revisions(
                entity_id=document.id, user_id=document.user_id, **pending.model_dump()
            ).insert()
        await self.model.get_pymongo_collection().update_one(
            {"_id": document.id, "pending_revision.revision_no": pending.revision_no},
            {"$set": {"pending_revision": None}},
        )
        document.pending_revision = None
        return document

    # ------------------------------------------------------------------ reads

    async def find(
        self, entity_id: str | PydanticObjectId, *, include_deleted: bool = False
    ) -> TRev | None:
        found = await super().find(entity_id, include_deleted=include_deleted)
        return None if found is None else await self._complete(found)

    async def history(
        self, entity_id: str | PydanticObjectId, params: PageParams
    ) -> tuple[list[Revision], int]:
        """The record's revisions, newest first (also for a deleted record)."""
        entity = await self.get(entity_id, include_deleted=True)
        mine = {"entity_id": entity.id}
        page = self.revisions.find(mine).sort("-revision_no").skip(params.skip)
        return await page.limit(params.page_size).to_list(), await self.revisions.find(mine).count()

    # Last of the reads: inside this class, `list` in a later annotation would mean this method.
    async def list(
        self,
        params: PageParams,
        sort: Sequence[tuple[str, int]],
        filters: Mapping[str, Any] | None = None,
        *,
        include_deleted: bool = False,
    ) -> tuple[list[TRev], int]:
        items, total = await super().list(params, sort, filters, include_deleted=include_deleted)
        return [await self._complete(item) for item in items], total

    # ------------------------------------------------------------------ writes

    async def create_once(
        self, fields: Mapping[str, Any], client_key: str | None, *, source: str = DEFAULT_SOURCE
    ) -> tuple[TRev, bool]:
        """Create the record, or return the one an earlier request with this key created.
        The second value says which: ``True`` = created now."""
        document = self.build({**fields, "client_key": client_key, "revision_no": 1})
        document.pending_revision = self._entry(
            1, RevisionAction.CREATE, [], self._fields(document), source
        )
        try:
            await document.insert()
        except DuplicateKeyError:
            earlier = client_key and await self.model.find_one(
                self.query({"client_key": client_key}, include_deleted=True)
            )
            if not earlier:
                raise
            return await self._complete(earlier), False
        return await self._complete(document), True

    async def create(self, fields: Mapping[str, Any], *, source: str = DEFAULT_SOURCE) -> TRev:
        return (await self.create_once(fields, None, source=source))[0]

    async def update(
        self,
        entity_id: str | PydanticObjectId,
        changes: Mapping[str, Any],
        *,
        expected: int | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> TRev:
        """Apply ``changes``. With ``expected`` (the revision the caller edited), a record that
        has moved on answers 409 instead of silently losing the other change."""
        return await self._revise(entity_id, changes, RevisionAction.UPDATE, expected, source)

    async def soft_delete(
        self, entity_id: str | PydanticObjectId, *, source: str = DEFAULT_SOURCE
    ) -> None:
        await self._revise(entity_id, {}, RevisionAction.DELETE, None, source)

    async def undelete(
        self, entity_id: str | PydanticObjectId, *, source: str = DEFAULT_SOURCE
    ) -> TRev:
        return await self._revise(entity_id, {}, RevisionAction.UNDELETE, None, source)

    async def restore(
        self, entity_id: str | PydanticObjectId, revision_no: int, *, source: str = DEFAULT_SOURCE
    ) -> TRev:
        """Put the record's fields back to how they were after ``revision_no``. This is a new
        revision: history is never rewritten."""
        current = await self.get(entity_id)
        earlier = await self.revisions.find_one(
            {"entity_id": current.id, "revision_no": revision_no}
        )
        if earlier is None:
            raise NotFoundError
        return await self._revise(entity_id, earlier.snapshot, RevisionAction.RESTORE, None, source)

    async def _revise(
        self,
        entity_id: str | PydanticObjectId,
        changes: Mapping[str, Any],
        action: RevisionAction,
        expected: int | None,
        source: str,
    ) -> TRev:
        reviving = action is RevisionAction.UNDELETE
        current = await self.get(entity_id, include_deleted=reviving)
        if reviving and current.deleted_at is None:
            return current
        if expected is not None and expected != current.revision_no:
            raise VersionConflictError(details={"current": current.revision_no})

        before = self._fields(current)
        after = self._fields(
            self.model.model_validate({**current.model_dump(by_alias=True), **changes})
        )
        diff = [
            FieldChange(field=name, old=before[name], new=value)
            for name, value in after.items()
            if before[name] != value
        ]
        if not diff and action in (RevisionAction.UPDATE, RevisionAction.RESTORE):
            return current  # nothing changed: no revision, no new version

        number = current.revision_no + 1
        entry = self._entry(number, action, diff, after, source)
        values: dict[str, Any] = {
            **{change.field: change.new for change in diff},
            "revision_no": number,
            "pending_revision": Encoder().encode(entry),
            "updated_at": entry.changed_at,
            "updated_by": self.scope.actor_id,
        }
        if action is RevisionAction.DELETE:
            values["deleted_at"] = entry.changed_at
        if reviving:
            values["deleted_at"] = None
        applied = await self.model.get_pymongo_collection().update_one(
            {
                "_id": current.id,
                **self.scope.owner_filter(),
                "revision_no": current.revision_no,
                "pending_revision": None,
            },
            {"$set": values},
        )
        if applied.matched_count == 0:
            raise VersionConflictError  # changed between our read and our write
        return await self.get(entity_id, include_deleted=action is RevisionAction.DELETE)
