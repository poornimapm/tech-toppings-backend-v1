"""Static mode for the test skill: "jot <text>" proposes a note."""

from __future__ import annotations

from app.platform import StaticOutcome, StaticRequest, ToolBox

PREFIX = "jot "


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    if not request.input.lower().startswith(PREFIX):
        return None
    tools.propose("notes_add", {"text": request.input[len(PREFIX) :]}, code="take-notes.change")
    return StaticOutcome("take-notes.ready")
