"""How well the rule parser reads labelled inputs: the numbers behind the CI gates,
``scripts/eval_parser.py`` and ``scripts/eval_receipts.py``. A case's format is described in
its data file."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from app.modules.expenses.parsing import Lexicon, parse
from app.modules.expenses.parsing.sentence import EXPENSE, Draft
from app.platform import normalise

FIELDS = ("entries", "amount", "item", "kind", "date", "quantity")
# A receipt: the total it read, whether the drafts add up to it, the items found (of those
# printed), the drafts that are right (of those made), the shop and the day.
RECEIPT_FIELDS = ("total", "sum", "items", "drafts", "shop", "date")
SAME_TITLE = 0.6  # how alike two titles must be (0 to 1) when neither holds the other
SAME_SHOP = 0.8
TOTALS = ("total", "subtotal", "amount", "amt")  # a balancing draft is never one of these
CENT = Decimal("0.01")


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


def report(scores: Mapping[str, Mapping[str, Score]], fields: Sequence[str] = FIELDS) -> str:
    """A Markdown table: one row per language, one column per field."""
    lines = [f"| | {' | '.join(fields)} |", f"|---|{'---|' * len(fields)}"]
    lines += [
        f"| {language} | {' | '.join(str(table[name]) for name in fields)} |"
        for language, table in scores.items()
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class ReceiptRead:
    """What a reader made of one receipt, whichever reader it was (rules or AI)."""

    drafts: list[tuple[str, Decimal]]  # (title, amount)
    merchant: str | None
    date: date | None
    total: Decimal | None = None  # only the rules report the total they read


def similar(read: str, want: str) -> bool:
    """Titles that name the same thing: one holds the other, or they are alike."""
    a, b = normalise(read), normalise(want)
    return bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= SAME_TITLE)


def same_shop(read: str, want: str) -> bool:
    """The shop as printed is in what was read ("D-Mart" in "D-Mart (Avenue Supermarts)"), or
    they are much alike: a bare "Store" is not "Gupta Medical Store"."""
    a, b = normalise(read), normalise(want)
    return bool(a and b) and (b in a or SequenceMatcher(None, a, b).ratio() >= SAME_SHOP)


def mark_receipt(
    read: ReceiptRead, truth: Mapping[str, Any], *, with_total: bool = True
) -> dict[str, tuple[int, int]]:
    """(right, out of) for each field. A draft is right when it is a printed item (its amount,
    a like title) or what makes the items add up to the total (a charge, the rest)."""
    total = Decimal(str(truth["total"]))
    items = [(str(title), Decimal(str(amount))) for title, amount in truth["items"]]
    balance = total - sum((amount for _, amount in items), Decimal(0))
    left = list(read.drafts)
    found = 0
    for title, amount in items:
        hit = next((d for d in left if d[1] == amount and similar(d[0], title)), None)
        if hit is not None:
            left.remove(hit)
            found += 1
    balancing = int(
        balance > 0
        and any(
            amount == balance and not any(word in normalise(title) for word in TOTALS)
            for title, amount in left
        )
    )
    paid = sum((amount for _, amount in read.drafts), Decimal(0))
    marks = {
        "sum": (int(abs(paid - total) < CENT), 1),
        "items": (found, len(items)),
        # No drafts at all is a miss, not nothing to score.
        "drafts": (found + balancing, max(len(read.drafts), 1)),
        "shop": (int(any(same_shop(read.merchant or "", shop) for shop in truth["merchant"])), 1),
        "date": (int(read.date == date.fromisoformat(truth["date"])), 1),
    }
    if with_total:
        marks["total"] = (int(read.total == total), 1)
    return marks


def score_receipts(
    reads: Iterable[tuple[Mapping[str, Any], ReceiptRead]], *, with_total: bool = True
) -> tuple[dict[str, dict[str, Score]], list[str]]:
    """Scores by language and field over (case, what was read) pairs, and a line per miss."""
    scores: dict[str, dict[str, Score]] = {}
    misses: list[str] = []
    for case, read in reads:
        marks = mark_receipt(read, case["truth"], with_total=with_total)
        table = scores.setdefault(case["lang"], {name: Score() for name in RECEIPT_FIELDS})
        for name, (right, total) in marks.items():
            table[name].right += right
            table[name].total += total
        wrong = [name for name, (right, total) in marks.items() if right < total]
        if wrong:
            drafts = ", ".join(f"{title!r} {amount}" for title, amount in read.drafts)
            misses.append(
                f"[{case['id']}] {', '.join(wrong)} -> total {read.total}, shop "
                f"{read.merchant!r}, date {read.date}: {drafts or 'no drafts'}"
            )
    return scores, misses
