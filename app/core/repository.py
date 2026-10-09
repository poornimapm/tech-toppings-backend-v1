"""User-scoped data access (brief §7.3).

Every query an ``ScopedRepository`` runs is ANDed with the caller's ``Scope``:

* ``Scope.for_user(id)`` -> ``{user_id: id}``  (all normal routes)
* ``Scope.unrestricted()`` -> no owner filter  (``/v1/admin/*`` only; an architecture test
  fails if it is used anywhere else)

plus ``deleted_at: null`` unless soft-deleted records are explicitly requested. There is no
method that bypasses the scope, so a forgotten filter cannot leak another user's data, and
records of other users answer exactly like missing ones (404, not 403).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from beanie import PydanticObjectId, SortDirection
from bson.errors import InvalidId

from app.core.clock import Clock
from app.core.documents import OwnedDocument
from app.core.errors import NotFoundError
from app.core.pagination import PageParams

TDoc = TypeVar("TDoc", bound=OwnedDocument)


@dataclass(frozen=True)
class Scope:
    actor_id: PydanticObjectId
    owner_id: PydanticObjectId | None  # None = unrestricted (admin routes only)

    @classmethod
    def for_user(cls, user_id: PydanticObjectId) -> Scope:
        return cls(actor_id=user_id, owner_id=user_id)

    @classmethod
    def unrestricted(cls, admin_id: PydanticObjectId) -> Scope:
        return cls(actor_id=admin_id, owner_id=None)

    def owner_filter(self) -> dict[str, Any]:
        return {} if self.owner_id is None else {"user_id": self.owner_id}


def parse_object_id(value: str | PydanticObjectId) -> PydanticObjectId | None:
    """Malformed ids are simply "not found" (no 422 that would reveal id structure)."""
    if isinstance(value, PydanticObjectId):
        return value
    try:
        return PydanticObjectId(value)
    except (InvalidId, TypeError):
        return None


class ScopedRepository(Generic[TDoc]):
    model: ClassVar[type[OwnedDocument]]

    def __init__(self, scope: Scope, clock: Clock) -> None:
        self.scope = scope
        self.clock = clock

    def query(
        self, extra: Mapping[str, Any] | None = None, *, include_deleted: bool = False
    ) -> dict[str, Any]:
        criteria: dict[str, Any] = {**(extra or {}), **self.scope.owner_filter()}
        if not include_deleted:
            criteria["deleted_at"] = None
        return criteria

    async def find(self, entity_id: str | PydanticObjectId) -> TDoc | None:
        object_id = parse_object_id(entity_id)
        if object_id is None:
            return None
        return await self.model.find_one(self.query({"_id": object_id}))

    async def get(self, entity_id: str | PydanticObjectId) -> TDoc:
        found = await self.find(entity_id)
        if found is None:
            raise NotFoundError
        return found

    async def list(
        self,
        params: PageParams,
        sort: Sequence[tuple[str, int]],
        filters: Mapping[str, Any] | None = None,
    ) -> tuple[list[TDoc], int]:
        criteria = self.query(filters)
        total = await self.model.find(criteria).count()
        items = (
            await self.model.find(criteria)
            .sort([(field, SortDirection(direction)) for field, direction in sort])
            .skip(params.skip)
            .limit(params.page_size)
            .to_list()
        )
        return items, total

    def module_key_for(self, fields: Mapping[str, Any]) -> str:  # noqa: ARG002 - override hook
        """Module owning a new record: the model's MODULE_KEY unless a subclass says otherwise."""
        return self.model.MODULE_KEY

    async def create(self, fields: Mapping[str, Any]) -> TDoc:
        """Validate and insert a record for the scope's owner. Ownership and audit fields are
        always set here (overriding anything in ``fields``), never trusted from the caller."""
        if self.scope.owner_id is None:
            raise ValueError("an unrestricted scope cannot create owned records")
        now = self.clock.now()
        document = self.model(
            **{
                **fields,
                "user_id": self.scope.owner_id,
                "module_key": self.module_key_for(fields),
                "created_by": self.scope.actor_id,
                "updated_by": self.scope.actor_id,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
        )
        await document.insert()
        return document

    async def update(self, entity_id: str | PydanticObjectId, changes: Mapping[str, Any]) -> TDoc:
        object_id = parse_object_id(entity_id)
        if object_id is None:
            raise NotFoundError
        values = {**changes, "updated_at": self.clock.now(), "updated_by": self.scope.actor_id}
        collection = self.model.get_pymongo_collection()
        result = await collection.update_one(self.query({"_id": object_id}), {"$set": values})
        if result.matched_count == 0:
            raise NotFoundError
        return await self.get(object_id)

    async def soft_delete(self, entity_id: str | PydanticObjectId) -> None:
        object_id = parse_object_id(entity_id)
        if object_id is None:
            raise NotFoundError
        now = self.clock.now()
        collection = self.model.get_pymongo_collection()
        result = await collection.update_one(
            self.query({"_id": object_id}),
            {"$set": {"deleted_at": now, "updated_at": now, "updated_by": self.scope.actor_id}},
        )
        if result.matched_count == 0:
            raise NotFoundError
