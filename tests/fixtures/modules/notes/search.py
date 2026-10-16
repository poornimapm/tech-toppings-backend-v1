"""The test module's search: notes whose text has the words. Some words make it misbehave, so
tests can see the platform leave a broken module out: "slow" outlasts the time limit, "boom"
fails, and "away" leads outside the module."""

from __future__ import annotations

import asyncio
import re

from app.platform import ActorContext, SearchHit, search
from tests.fixtures.modules.notes.models import NoteRepository

SLOW_SECONDS = 30


@search
async def notes_search(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    if "slow" in text:
        await asyncio.sleep(SLOW_SECONDS)
    if "boom" in text:
        raise RuntimeError("the notes search broke")
    repo = NoteRepository(ctx.scope, ctx.clock)
    criteria = {"text": {"$regex": re.escape(text), "$options": "i"}}
    found = await repo.model.find(repo.query(criteria)).limit(limit + 5).to_list()
    path = "https://elsewhere.example/" if "away" in text else None
    return [
        SearchHit(kind="note", id=str(note.id), title=note.text, path=path or f"notes/{note.id}")
        for note in found
    ]
