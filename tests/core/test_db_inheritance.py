"""Documents defined after Beanie was initialised (e.g. a module loaded by a second app in the
same process) must still get correct field defaults."""

from __future__ import annotations

from typing import ClassVar

from beanie import PydanticObjectId

from app.core.db import Database
from app.core.documents import OwnedDocument
from tests.helpers import FrozenClock, mongo_settings


class EarlyThing(OwnedDocument):
    MODULE_KEY: ClassVar[str] = "early"

    class Settings:
        name = "early_things"


async def test_documents_defined_after_initialisation_keep_their_defaults(mongo_uri: str) -> None:
    first = Database(mongo_settings(mongo_uri), [EarlyThing])
    assert await first.check_ready()
    await first.close()

    class LateThing(OwnedDocument):
        MODULE_KEY: ClassVar[str] = "late"

        class Settings:
            name = "late_things"

    second = Database(mongo_settings(mongo_uri), [EarlyThing, LateThing])
    try:
        assert await second.check_ready()
        now, owner = FrozenClock().now(), PydanticObjectId()
        thing = LateThing(
            user_id=owner, module_key="late", created_by=owner, updated_by=owner,
            created_at=now, updated_at=now,
        )  # fmt: skip
        assert thing.id is None
        assert thing.deleted_at is None
        inserted = await thing.insert()
        assert isinstance(inserted.id, PydanticObjectId)
        assert str(LateThing.user_id) == "user_id"  # query syntax still works on real documents
    finally:
        await second.close()
