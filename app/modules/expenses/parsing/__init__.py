"""The rule parser: free text (typed, spoken or a pasted bank SMS) to draft entries, or loans
(``ious.py``), with no AI. It is the static mode of the track-expenses skill, and what answers
when AI can't. ``receipt.py`` reads a shop receipt's text the same way (the scan-receipt skill).

Everything it knows about languages, units, categories and SMS formats is data in the skills'
``references/`` folders; ``scripts/eval_parser.py`` and ``scripts/eval_receipts.py`` measure it
against labelled inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from datetime import date
from typing import Any

from app.modules.expenses.parsing import sentence, sms
from app.modules.expenses.parsing.ious import IouDraft, load_ious, read_ious
from app.modules.expenses.parsing.lexicon import Lexicon, load
from app.modules.expenses.parsing.receipt import Receipt, ReceiptWords, load_receipt, read_receipt
from app.modules.expenses.parsing.sentence import Draft, has_money_words

__all__ = [
    "Draft",
    "IouDraft",
    "Lexicon",
    "Receipt",
    "ReceiptWords",
    "arguments",
    "load",
    "load_ious",
    "load_receipt",
    "parse",
    "read_ious",
    "read_receipt",
    "speaks_of_money",
]


def speaks_of_money(text: str, lexicon: Lexicon) -> bool:
    """Is this about money at all? (So "pin 2 modules" isn't read as an expense of 2.)"""
    return sms.looks_like(text, lexicon) or has_money_words(text, lexicon)


def parse(
    text: str,
    today: date,
    lexicon: Lexicon,
    *,
    habits: Mapping[str, str] | None = None,
    categories: Mapping[str, Set[str]] | None = None,
) -> list[Draft]:
    """``habits``: the user's usual category by (normalised) title. ``categories``: the ids
    they may use, by kind."""
    if sms.looks_like(text, lexicon):
        return [sms.parse(text, today, lexicon, categories or {})]
    return sentence.parse(text, today, lexicon, habits or {}, categories or {})


def arguments(draft: Draft, uncertain_below: float) -> dict[str, Any]:
    """A draft as the ``expenses_draft`` client tool takes it, with the fields the parser is
    unsure of marked for the user to check."""
    return {
        "kind": draft.kind,
        "title": draft.title,
        "amount": float(draft.amount) if draft.amount is not None else None,
        "category_id": draft.category_id,
        "date": draft.date.isoformat() if draft.date else None,
        "merchant": draft.merchant,
        "quantity": float(draft.quantity) if draft.quantity is not None else None,
        "unit": draft.unit,
        "uncertain": [name for name, sure in draft.confidence.items() if sure < uncertain_below],
    }
