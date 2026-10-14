"""How well the rule parser reads labelled inputs: the numbers behind the CI gate and
``scripts/eval_parser.py``. A case's format is described in its data file."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.modules.expenses.parsing import Lexicon, parse
from app.modules.expenses.parsing.sentence import EXPENSE, Draft
from app.platform import normalise

FIELDS = ("entries", "amount", "item", "kind", "date", "quantity")


@dataclass
class Score:
    right: int = 0
    total: int = 0

    @property
    def rate(self) -> float:
        return self.right / self.total if self.total else 1.0

    def __str__(self) -> str:
        return f"{self.right}/{self.total} ({self.rate:.0%})" if self.total else "-"


def _marks(drafts: list[Draft], want: list[dict[str, Any]], today: date) -> dict[str, bool]:
    """Which fields were read right. A field the case doesn't label isn't marked at all."""
    pairs = list(zip(drafts, want, strict=False))
    same = len(drafts) == len(want)
    marks = {
        "entries": same,
        "amount": same and all(d.amount == Decimal(str(w["amount"])) for d, w in pairs),
        "kind": same and all(d.kind == w.get("kind", EXPENSE) for d, w in pairs),
    }
    if all("item" in w for w in want):
        marks["item"] = same and all(
            normalise(str(w["item"])) in normalise(d.title or "") for d, w in pairs
        )
    if any("qty" in w for w in want):
        marks["quantity"] = same and all(
            (d.quantity, d.unit) == (Decimal(str(w["qty"][0])), w["qty"][1])
            for d, w in pairs
            if "qty" in w
        )
    if any("ago" in w for w in want):
        marks["date"] = same and all(
            d.date == today - timedelta(days=w["ago"]) for d, w in pairs if "ago" in w
        )
    return marks


def score(
    cases: Iterable[Mapping[str, Any]],
    lexicon: Lexicon,
    today: date,
    categories: Mapping[str, Set[str]],
) -> tuple[dict[str, dict[str, Score]], list[str]]:
    """Scores by language and field, and a line for every miss."""
    scores: dict[str, dict[str, Score]] = {}
    misses: list[str] = []
    for case in cases:
        drafts = parse(case["text"], today, lexicon, categories=categories)
        marks = _marks(drafts, case["want"], today)
        table = scores.setdefault(case["lang"], {name: Score() for name in FIELDS})
        for name, right in marks.items():
            table[name].total += 1
            table[name].right += right
        wrong = [name for name, right in marks.items() if not right]
        if wrong:
            read = [
                (str(d.amount), d.title, d.kind, str(d.date), d.quantity, d.unit) for d in drafts
            ]
            misses.append(f"[{case['lang']}] {case['text']!r}: {', '.join(wrong)} -> {read}")
    return scores, misses


def report(scores: Mapping[str, Mapping[str, Score]]) -> str:
    """A Markdown table: one row per language, one column per field."""
    lines = [f"| | {' | '.join(FIELDS)} |", f"|---|{'---|' * len(FIELDS)}"]
    lines += [
        f"| {language} | {' | '.join(str(table[name]) for name in FIELDS)} |"
        for language, table in scores.items()
    ]
    return "\n".join(lines)
