"""Static mode for track-expenses, driven by references/lexicon.yaml."""

from __future__ import annotations

from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
)


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    found = match_action(normalise(request.input), load_lexicon(request.references))
    if found is None:
        return None
    action, entry = found
    await tools.client(entry["tool"], {})
    return StaticOutcome(f"track-expenses.ready.{action}")
