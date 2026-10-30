"""Static mode for mark-attendance: the day's marks said or typed, read by the rules (the
module's marks reader) against the project's workers, and handed to the page as a draft the
owner checks and saves. Asked from anywhere but the attendance page, it reads only words that
say a mark, and only for the one open project."""

from __future__ import annotations

from datetime import date

from app.modules.labor.marks_reader import (
    day_said,
    load_words,
    read_marks,
    roster_of,
    speaks_of_marks,
)
from app.platform import StaticOutcome, StaticRequest, ToolBox


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    words = load_words(request.references)
    if not request.chosen and not speaks_of_marks(request.input, words):
        return None  # other skills' words ("Ravi paid 500")
    given = request.context.get("day")
    sheet = await tools.read(
        "labor_day", {"project_id": request.context.get("project_id"), "day": given}
    )
    if "choose" in sheet:
        code = "mark-attendance.which_project" if sheet["choose"] else "mark-attendance.no_project"
        return StaticOutcome(code, tentative=not request.chosen)
    draft = read_marks(request.input, roster_of(sheet["workers"]), words)
    if draft.empty:
        return StaticOutcome("mark-attendance.nothing") if request.chosen else None
    day = day_said(draft, request.today, date.fromisoformat(given) if given else None)
    await tools.client(words.tool, {"draft": draft.arguments(sheet["project_id"], day)})
    unsure = sum(1 for mark in draft.marks if mark.uncertain)
    if unsure:
        return StaticOutcome("mark-attendance.unsure", {"count": unsure})
    return StaticOutcome("mark-attendance.ready", {"count": len(draft.marks)})
