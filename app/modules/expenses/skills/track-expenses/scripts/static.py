"""Static mode for track-expenses: the lexicon's actions, then the rule parser."""

from __future__ import annotations

from typing import Any

from app.modules.expenses import parsing
from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
)


def _argument(draft: parsing.Draft, uncertain_below: float) -> dict[str, Any]:
    return {
        "kind": draft.kind,
        "title": draft.title,
        "amount": float(draft.amount) if draft.amount is not None else None,
        "category_id": draft.category_id,
        "date": draft.date.isoformat() if draft.date else None,
        "merchant": draft.merchant,
        "uncertain": [name for name, sure in draft.confidence.items() if sure < uncertain_below],
    }


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    lexicon = load_lexicon(request.references)
    if found := match_action(normalise(request.input), lexicon):
        action, entry = found
        await tools.client(entry["tool"], {})
        return StaticOutcome(f"track-expenses.ready.{action}")

    words = parsing.load(request.references)
    # Asked from the Expenses page, anything typed is an entry. Asked from the general
    # assistant, only what speaks of money is: "pin 2 modules" is not an expense of 2.
    asked_here = request.context.get("input_type") is not None
    if not asked_here and not parsing.speaks_of_money(request.input, words):
        return None

    categories: dict[str, set[str]] = {}
    for category in await tools.read("expenses_categories", {}):
        categories.setdefault(category["kind"], set()).add(category["id"])
    habits = {
        normalise(habit["title"]): habit["category_id"]
        for habit in await tools.read("expenses_habits", {})
    }
    drafts = parsing.parse(
        request.input, request.today, words, habits=habits, categories=categories
    )
    if not drafts or not (asked_here or any(draft.amount for draft in drafts)):
        return None  # from the general assistant, "buy milk" is a to-do, not an expense
    arguments = [_argument(draft, words.uncertain_below) for draft in drafts]
    await tools.client(lexicon["capture"]["tool"], {"drafts": arguments})
    return StaticOutcome("track-expenses.ready.draft")
