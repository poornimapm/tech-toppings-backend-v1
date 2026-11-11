"""Static mode for activity-checklist: with no AI there are no AI items, only the app's own rule
suggestions for the activity's kind (data/travel.yaml). It says so by its code, and the page shows
them through the rule suggestions' own preview (``/checklist/suggest`` with the activity), so they
are stored as what they are: suggestions by rule, never the assistant's."""

from __future__ import annotations

from app.modules.travel.rules import rules
from app.platform import StaticOutcome, StaticRequest, ToolBox


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    activity_id = request.context.get("activity_id")
    if not request.chosen or not activity_id:
        return None  # only from an activity's own button
    found = await tools.read("travel_activity", {"activity_id": activity_id})
    activity = found["activity"]
    params = {"trip_id": found["trip_id"], "activity_id": activity["id"], "day": activity["day"]}
    if not rules().suggestions.activities.get(activity["kind"]):
        return StaticOutcome("activity-checklist.no_rules", params)
    return StaticOutcome("activity-checklist.rules", params)
