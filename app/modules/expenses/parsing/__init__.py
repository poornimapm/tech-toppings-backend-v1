"""The rule parser: free text (typed, spoken or a pasted bank SMS) to draft entries, with no
AI. It is the static mode of the track-expenses skill, and what answers when AI can't.

Everything it knows about languages, units, categories and SMS formats is data in the skill's
``references/`` folder; ``scripts/eval_parser.py`` measures it against labelled inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from datetime import date

from app.modules.expenses.parsing import sentence, sms
from app.modules.expenses.parsing.lexicon import Lexicon, load
from app.modules.expenses.parsing.sentence import Draft, has_money_words

__all__ = ["Draft", "Lexicon", "load", "parse", "speaks_of_money"]


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
