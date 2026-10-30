"""Static mode for track-expenses: the lexicon's actions, then loans, then the rule parser."""

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


def _unsure(confidence: dict[str, float], uncertain_below: float) -> list[str]:
    return [name for name, sure in confidence.items() if sure < uncertain_below]


def _loan(loan: parsing.IouDraft, uncertain_below: float) -> dict[str, Any]:
    """A loan as the client tool takes it. The due date is left for the user: the parser never
    reads a day to come."""
    return {
        "action": loan.action,
        "direction": loan.direction,
        "counterparty": loan.counterparty,
        "amount": float(loan.amount) if loan.amount is not None else None,
        "date": loan.date.isoformat() if loan.date else None,
        "uncertain": _unsure(loan.confidence, uncertain_below),
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
    # Only explicit loan words count ("lent", "उधार दिए"), so "gave Ravi 500" stays an expense;
    # a new loan from anywhere, since it needs no money word ("lent Ravi 500"), but being paid
    # back only here: elsewhere "back" is seldom about money ("I'll be back in 10 minutes").
    loans = parsing.read_ious(
        request.input,
        request.today,
        words,
        parsing.load_ious(request.references),
        repayments=asked_here,
    )
    if any(loan.amount for loan in loans):
        arguments = [_loan(loan, words.uncertain_below) for loan in loans]
        await tools.client(lexicon["capture"]["iou_tool"], {"drafts": arguments})
        return StaticOutcome("track-expenses.ready.iou")

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
    arguments = [parsing.arguments(draft, words.uncertain_below) for draft in drafts]
    await tools.client(lexicon["capture"]["tool"], {"drafts": arguments})
    return StaticOutcome("track-expenses.ready.draft")
