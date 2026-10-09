from __future__ import annotations

from typing import ClassVar

from app.platform import OwnedDocument, ScopedRepository


class Note(OwnedDocument):
    MODULE_KEY: ClassVar[str] = "notes"

    text: str

    class Settings:
        name = "notes"


class NoteRepository(ScopedRepository[Note]):
    model: ClassVar[type[OwnedDocument]] = Note
