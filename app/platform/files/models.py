"""Small private files a module keeps for its owner (a worker's photo, a picture of an ID).

One document per file, its bytes inside: files here are a few hundred KB at most, and MongoDB
advises single documents for files well under its 16 MB limit (GridFS is for larger ones). So a
file is scoped, exported and erased like any other owned record.
"""

from __future__ import annotations

from typing import ClassVar

from pymongo import ASCENDING, IndexModel

from app.core.documents import OwnedDocument


class StoredFile(OwnedDocument):
    MODULE_KEY: ClassVar[str] = "platform"  # the owning module is in module_key, per file
    # The export lists each file (what, how large); its bytes are fetched from the module.
    PRIVATE_FIELDS: ClassVar[frozenset[str]] = frozenset({"data"})

    purpose: str  # the module's name for it, e.g. "worker_photo"
    content_type: str  # taken from the bytes, never from the client
    size: int
    sha256: str
    data: bytes

    class Settings:
        name = "stored_files"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("user_id", ASCENDING), ("module_key", ASCENDING)], name="owner_module"),
        ]
