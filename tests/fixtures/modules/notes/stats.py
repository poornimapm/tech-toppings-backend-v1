from __future__ import annotations

from app.platform import Clock, Scope, TileStat
from tests.fixtures.modules.notes.models import Note, NoteRepository


async def note_count(scope: Scope, clock: Clock) -> TileStat | None:
    count = await Note.find(NoteRepository(scope, clock).query()).count()
    return TileStat(value=count, label="stat_notes")
