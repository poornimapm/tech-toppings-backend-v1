"""Static mode for explain-spending: the period's facts, said with the skill's templates (the
module's manifest words each code)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
    to_minor,
)

DEFAULT_PERIOD = "month"


def trend(facts: Mapping[str, Any], steady_pct: float) -> str:
    """How the period compares with the same days before."""
    if not facts["spent"]:
        return "empty"
    change = facts["spent_change_pct"]
    if change is None:
        return "first"
    if abs(change) <= steady_pct:
        return "same"
    return "more" if change > 0 else "less"


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    lexicon = load_lexicon(request.references)
    found = match_action(normalise(request.input), lexicon)
    if found is None and not request.chosen:
        return None
    period = request.context.get("period") or (found[0] if found else DEFAULT_PERIOD)
    day = request.context.get("date") or request.today.isoformat()
    facts = await tools.read("expenses_insight_facts", {"period": period, "date": day})
    currency, top = facts["currency"], (facts["top_categories"] or [None])[0]
    change = facts["spent_change_pct"]
    params = {
        "currency": currency,
        "spent_minor": to_minor(facts["spent"], currency),
        "pct": round(abs(change)) if change is not None else None,
        "category": top and top["category"],
        "category_amount_minor": top and to_minor(top["amount"], currency),
    }
    steady = float(lexicon["summary"]["steady_pct"])
    return StaticOutcome(f"explain-spending.{period}.{trend(facts, steady)}", params)
