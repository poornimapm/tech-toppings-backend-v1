"""The test module's agent tool: a write, so tests can exercise proposals with a module."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.platform import ActorContext, tool
from tests.fixtures.modules.notes.models import NoteRepository


@tool("write")
async def notes_add(
    ctx: ActorContext, text: Annotated[str, Field(min_length=1, max_length=200)]
) -> dict[str, str]:
    """Save a note for the user."""
    note = await NoteRepository(ctx.scope, ctx.clock).create({"text": text})
    return {"id": str(note.id)}
