from __future__ import annotations

from beanie import Document

from app.core.db import Database
from tests.helpers import UNREACHABLE_MONGO_URI, mongo_settings


class ProbeDocument(Document):
    name: str

    class Settings:
        name = "probe_documents"


async def test_check_ready_initialises_beanie_once_and_documents_work(mongo_uri: str) -> None:
    database = Database(mongo_settings(mongo_uri), document_models=[ProbeDocument])
    try:
        ready_before_check = (
            database.odm_ready
        )  # local copy: keeps mypy from narrowing the property
        assert ready_before_check is False

        assert await database.check_ready()
        assert database.odm_ready
        assert await database.check_ready()  # second call: ping only, no re-initialisation

        await ProbeDocument(name="hello").insert()
        found = await ProbeDocument.find_one(ProbeDocument.name == "hello")
        assert found is not None
        assert found.name == "hello"
    finally:
        await database.close()


async def test_check_ready_is_false_and_does_not_raise_when_unreachable() -> None:
    database = Database(
        mongo_settings(UNREACHABLE_MONGO_URI, server_selection_timeout_ms=200), document_models=[]
    )
    try:
        assert await database.check_ready() is False
        assert database.odm_ready is False
        assert database.name == "tt-unreachable"
    finally:
        await database.close()
