"""Static mode for check-notifications, driven by references/lexicon.yaml."""

from __future__ import annotations

from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
)

MOST = 50  # the tool's own limit: "50 or more" is enough to say how much is waiting


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    found = match_action(normalise(request.input), load_lexicon(request.references))
    if found is None:
        return None
    action, entry = found
    unread = len(await tools.read("notifications_list", {"unread_only": True, "limit": MOST}))
    params = {"count": unread}

    if tools.tools[entry["tool"]].effect == "client":
        await tools.client(entry["tool"], {})
    elif unread == 0:
        return StaticOutcome("check-notifications.none-unread")
    else:
        tools.propose(entry["tool"], {}, code=f"check-notifications.change.{action}", params=params)
    return StaticOutcome(f"check-notifications.ready.{action}", params)
