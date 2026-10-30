"""Static mode for scan-receipt: a receipt's text, read on the device, through the receipt
reader. A photo can't be read without AI: the app is told to read it on the device first."""

from __future__ import annotations

from app.modules.expenses import parsing
from app.modules.expenses.tools import MAX_ENTRY_DRAFTS
from app.platform import StaticOutcome, StaticRequest, ToolBox, normalise


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    if not request.chosen:
        return None  # only the scan asks: any other text is the other skills' to read
    if not request.input.strip():
        return StaticOutcome("scan-receipt.on_device")
    words = parsing.load_receipt(request.references)
    # The expenses words (categories, units, months) are the track-expenses skill's.
    lexicon = parsing.load(request.references.parent.parent / "track-expenses" / "references")
    categories: dict[str, set[str]] = {}
    for category in await tools.read("expenses_categories", {}):
        categories.setdefault(category["kind"], set()).add(category["id"])
    habits = {
        normalise(habit["title"]): habit["category_id"]
        for habit in await tools.read("expenses_habits", {})
    }
    receipt = parsing.read_receipt(
        request.input,
        request.today,
        lexicon,
        words,
        locale=request.locale,
        max_drafts=MAX_ENTRY_DRAFTS,
        habits=habits,
        categories=categories,
    )
    if not receipt.drafts:
        return StaticOutcome("scan-receipt.unreadable")
    drafts = [parsing.arguments(draft, lexicon.uncertain_below) for draft in receipt.drafts]
    await tools.client(words.tool, {"drafts": drafts})
    total = {"total": float(receipt.total)} if receipt.total is not None else {}
    return StaticOutcome("scan-receipt.ready", {"count": len(drafts), **total})
