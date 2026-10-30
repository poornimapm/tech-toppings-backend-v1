"""A bank or UPI message as one draft entry. The formats are data (references/sms.yaml)."""

from __future__ import annotations

from collections.abc import Mapping, Set
from datetime import date
from decimal import Decimal

from app.modules.expenses.parsing.lexicon import Lexicon, known, lookup
from app.modules.expenses.parsing.sentence import EXPENSE, INCOME, Draft
from app.platform import normalise, on_or_before


def _first(patterns: tuple, text: str) -> tuple[str, ...] | None:  # type: ignore[type-arg]
    return next((found.groups() for pattern in patterns if (found := pattern.search(text))), None)


def _position(text: str, words: Set[str]) -> int:
    """Where the earliest of the words is, or past the end when there is none."""
    return min((at for word in words if (at := text.find(word)) >= 0), default=len(text))


def looks_like(text: str, lexicon: Lexicon) -> bool:
    plain = normalise(text)
    hits = sum(word in plain for word in lexicon.sms.detect)
    return hits >= lexicon.sms.min_hits and _first(lexicon.sms.amount, text) is not None


def _date(text: str, today: date, lexicon: Lexicon) -> date | None:
    found = _first(lexicon.sms.date, text)
    if found is None:
        return None
    day, month, year = found
    named = lookup(normalise(month), lexicon.months)
    number = int(month) if month.isdigit() else (named + 1 if named is not None else 0)
    when = on_or_before(today, int(day), number, int(year)) if number else None
    return when if when and when <= today else None


def parse(text: str, today: date, lexicon: Lexicon, categories: Mapping[str, Set[str]]) -> Draft:
    plain = normalise(text)
    sure = lexicon.confidence
    amount = _first(lexicon.sms.amount, text)
    credit, debit = _position(plain, lexicon.sms.credit), _position(plain, lexicon.sms.debit)
    kind = INCOME if credit < debit else EXPENSE
    merchant = _first(lexicon.sms.merchant, text)
    name = merchant[0].strip(" .-") if merchant else None
    words = normalise(name or "").split()
    usable = categories.get(kind, frozenset())
    category = next(
        (
            key
            for key, keywords in lexicon.keywords.items()
            if key in usable and any(known(word, keywords) for word in words)
        ),
        None,
    )
    fallback = lexicon.defaults.get(kind)
    day = _date(text, today, lexicon)
    return Draft(
        kind=kind,
        title=name,
        amount=Decimal(amount[0].replace(",", "")) if amount else None,
        category_id=category or (fallback if fallback in usable else None),
        date=day,
        merchant=name,
        confidence={
            "amount": sure["amount_marked"] if amount else 0.0,
            "title": sure["title"] if name else 0.0,
            "category_id": sure["category_keyword"] if category else sure["category_default"],
            "date": sure["date_said"] if day else sure["date_assumed"],
        },
    )
