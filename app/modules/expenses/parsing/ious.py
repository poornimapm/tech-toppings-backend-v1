"""Loans in what someone said ("lent Ravi 500", "रवि से 2000 उधार लिया"): new money lent or
borrowed, or a payment on it, with who, how much and the day.

Only the loan words in ``references/ious.yaml`` make a sentence a loan; the amount, the day and
the words left for the person's name come from the rule parser, as for an entry. Each part of
the sentence is read on its own, so "lent Ravi 500 and borrowed 200 from Meena" is two loans,
one each way.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.modules.expenses.parsing import sentence, sms
from app.modules.expenses.parsing.lexicon import STEM_LENGTH, Lexicon, known
from app.platform import normalise

NEW = "new"
# A cue: one word or phrase, or several that must all be said.
Cue = tuple[str, ...]


@dataclass(frozen=True)
class LoanAction:
    action: str  # new | repay
    direction: str  # lent | borrowed
    sure: bool  # do the words say who gave? ("Ravi lent 500" doesn't say to whom)
    cues: tuple[Cue, ...]


@dataclass(frozen=True)
class LoanWords:
    actions: tuple[LoanAction, ...]
    not_loans: tuple[str, ...]
    strip: frozenset[str]
    endings: tuple[tuple[str, str], ...]  # longest first
    confidence: dict[str, float]


@dataclass
class IouDraft:
    """One loan as understood, with how sure the reading is of each field (0 to 1)."""

    action: str
    direction: str
    counterparty: str | None = None
    amount: Decimal | None = None
    date: date | None = None
    confidence: dict[str, float] = field(default_factory=dict)


def _cue(item: str | Sequence[str]) -> Cue:
    return (normalise(item),) if isinstance(item, str) else tuple(normalise(w) for w in item)


@cache
def load_ious(folder: Path) -> LoanWords:
    data: dict[str, Any] = yaml.safe_load((folder / "ious.yaml").read_text(encoding="utf-8"))
    return LoanWords(
        actions=tuple(
            LoanAction(
                entry["action"],
                entry["direction"],
                entry.get("sure", True),
                tuple(_cue(c) for c in entry["cues"]),
            )
            for entry in data["actions"].values()
        ),
        not_loans=tuple(normalise(word) for word in data["not_loans"]),
        strip=frozenset(normalise(word) for word in data["strip"]),
        endings=tuple(sorted(data["endings"].items(), key=lambda pair: -len(pair[0]))),
        confidence={name: float(value) for name, value in data["confidence"].items()},
    )


def _said(text: str, word: str) -> bool:
    """Is the word in the normalised text? "^" means at the start, and "*" stands for any one
    word ("paid * back"). English matches whole words; a Tamil or Hindi word also matches with
    an ending attached ("திருப்பிக்", "लौटाए"), unless it is too short to be a stem ("ने",
    "को": whole words, so "मैंने" is not "ने")."""
    start, word = word.startswith("^"), word.removeprefix("^")
    if word.isascii():
        body = " ".join(r"\S+" if part == "*" else re.escape(part) for part in word.split())
        return re.search(("^" if start else r"(?<!\w)") + body + r"(?!\w)", text) is not None
    if len(word) < STEM_LENGTH:
        tokens = text.split()
        return tokens[:1] == [word] if start else word in tokens
    return text.startswith(word) if start else word in text


def _action(said: str, words: LoanWords, repayments: bool) -> LoanAction | None:
    """The first action with a cue said (ious.yaml lists the more specific ones first)."""
    plain = normalise(said)
    return next(
        (
            action
            for action in words.actions
            if (repayments or action.action == NEW)
            and any(all(_said(plain, word) for word in cue) for cue in action.cues)
        ),
        None,
    )


def _carried(found: list[LoanAction | None]) -> list[LoanAction]:
    """A part with no loan word of its own is the same kind of loan as the part before it
    ("lent Ravi 500 and 300 to Priya"), or as the first one after it."""
    first = next(action for action in found if action is not None)
    carried: list[LoanAction] = []
    for action in found:
        carried.append(action or (carried[-1] if carried else first))
    return carried


def _spends(said: str, lexicon: Lexicon) -> bool:
    """Does the part say money was spent or received ("paid 300 for lunch")?"""
    signals = lexicon.spend | lexicon.income
    return any(known(word, signals) for word in normalise(said).split())


def _person(draft: sentence.Draft, words: LoanWords) -> str | None:
    """Where the money came from ("from Ravi"), else the words left once the loan words are
    out, without a Tamil case ending."""
    for said in (draft.merchant, draft.title):
        kept = [word for word in (said or "").split() if not known(normalise(word), words.strip)]
        if not kept:
            continue
        last = kept[-1]
        for ending, rest in words.endings:
            if last.endswith(ending) and len(last) > len(ending):
                kept[-1] = last[: -len(ending)] + rest
                break
        return " ".join(kept)
    return None


def read_ious(
    text: str, today: date, lexicon: Lexicon, words: LoanWords, *, repayments: bool = True
) -> list[IouDraft]:
    """The loans said, one per amount; none when no loan word is said, when a word says it
    isn't one (a refund, "not"), or when a part is spending of its own ("lent Ravi 500 and paid
    300 for lunch" stays entries, for the user to sort out). A bank message is always one entry.
    ``repayments``: whether being paid back counts too, or only new loans."""
    plain = normalise(text)
    if sms.looks_like(text, lexicon) or any(_said(plain, word) for word in words.not_loans):
        return []
    drafts = sentence.parse(text, today, lexicon, {}, {})
    found = [_action(draft.said, words, repayments) for draft in drafts]
    spending = any(
        action is None and _spends(draft.said, lexicon)
        for action, draft in zip(found, drafts, strict=True)
    )
    if spending or not any(found):
        return []
    sure = words.confidence
    return [
        IouDraft(
            action=action.action,
            direction=action.direction,
            counterparty=(person := _person(draft, words)),
            amount=draft.amount,
            date=draft.date,
            confidence={
                "amount": draft.confidence["amount"],
                "date": draft.confidence["date"],
                "direction": sure["direction_said" if action.sure else "direction_assumed"],
                "counterparty": sure["counterparty"] if person else 0.0,
            },
        )
        for action, draft in zip(_carried(found), drafts, strict=True)
    ]
