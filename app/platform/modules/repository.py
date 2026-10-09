from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.documents import OwnedDocument
from app.core.repository import ScopedRepository
from app.platform.modules.models import UserModule

# Values a preference record starts with; the fields being written win over these.
_INITIAL_VALUES: dict[str, Any] = {
    "enabled": None,
    "pinned": False,
    "position": None,
    "settings": {},
    "notify_requested_at": None,
}


class UserModuleRepository(ScopedRepository[UserModule]):
    model: ClassVar[type[OwnedDocument]] = UserModule

    def module_key_for(self, fields: Mapping[str, Any]) -> str:
        return str(fields["module_key"])

    async def by_module(self) -> dict[str, UserModule]:
        documents = await UserModule.find(self.query()).to_list()
        return {document.module_key: document for document in documents}

    async def for_module(self, module_key: str) -> UserModule | None:
        return await UserModule.find_one(self.query({"module_key": module_key}))

    async def upsert(self, module_key: str, values: Mapping[str, Any]) -> UserModule:
        """Write some preference fields for one module, creating the record on first use.

        A single atomic upsert, so two tabs saving at once can't create duplicates; the unique
        index turns the rare simultaneous insert into DuplicateKeyError, retried as an update."""
        if self.scope.owner_id is None:
            raise ValueError("an unrestricted scope cannot write preferences")
        now = self.clock.now()
        initial = {key: value for key, value in _INITIAL_VALUES.items() if key not in values}
        update = {
            "$set": {**values, "updated_at": now, "updated_by": self.scope.actor_id},
            "$setOnInsert": {**initial, "created_at": now, "created_by": self.scope.actor_id},
        }
        collection = UserModule.get_pymongo_collection()
        criteria = self.query({"module_key": module_key})
        try:
            raw = await collection.find_one_and_update(
                criteria, update, upsert=True, return_document=ReturnDocument.AFTER
            )
        except DuplicateKeyError:  # pragma: no cover - two first writes in the same instant
            raw = await collection.find_one_and_update(  # it exists now: a plain update
                criteria, update, return_document=ReturnDocument.AFTER
            )
        document: UserModule = UserModule.model_validate(raw)
        return document
