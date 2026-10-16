"""Loans in what was said (the track-expenses static mode): the accuracy gate over the labelled
loan sentences, and the sentences that must stay expenses."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.modules.expenses import parsing
from app.platform import normalise
from tests.modules.expenses.test_parser import LEXICON, REFERENCES, TODAY, cases

LOANS = parsing.load_ious(REFERENCES)
# Per language: how often each field must be read right.
TARGETS = {"action": 0.9, "direction": 0.9, "person": 0.8, "amount": 0.95}


def read(text: str, *, repayments: bool = True) -> list[parsing.IouDraft]:
    return parsing.read_ious(text, TODAY, LEXICON, LOANS, repayments=repayments)


def said(text: str) -> list[tuple[str, str, str | None, str]]:
    return [(d.action, d.direction, d.counterparty, str(d.amount)) for d in read(text)]


def marks(case: dict[str, Any]) -> dict[str, bool]:
    """Which fields of a case were read right; all wrong unless exactly one loan was read."""
    loans = read(case["text"])
    if len(loans) != 1:
        return dict.fromkeys(TARGETS, False)
    (loan,) = loans
    return {
        "action": loan.action == case["action"],
        "direction": loan.direction == case["direction"],
        "person": normalise(loan.counterparty or "") == normalise(str(case["person"])),
        "amount": loan.amount == Decimal(str(case["amount"])),
    }


def test_there_are_loan_sentences_in_every_language() -> None:
    counts: dict[str, int] = {}
    for case in cases("iou_cases.yaml"):
        counts[case["lang"]] = counts.get(case["lang"], 0) + 1

    assert counts == {"en": 17, "ta": 12, "hi": 12, "mixed": 7}


def test_loans_are_read_well_enough_in_every_language() -> None:
    right: dict[str, dict[str, list[bool]]] = {}
    misses: list[str] = []
    for case in cases("iou_cases.yaml"):
        found = marks(case)
        table = right.setdefault(case["lang"], {name: [] for name in TARGETS})
        for name, ok in found.items():
            table[name].append(ok)
        if not all(found.values()):
            misses.append(f"[{case['lang']}] {case['text']!r} -> {said(case['text'])}")

    for language, table in right.items():
        for name, target in TARGETS.items():
            rate = sum(table[name]) / len(table[name])
            assert rate >= target, f"{language} {name} {rate:.0%}\n" + "\n".join(misses)


@pytest.mark.parametrize(
    "name", ["parser_cases.yaml", "parser_cases_unseen.yaml", "iou_near_misses.yaml"]
)
def test_no_entry_sentence_reads_as_a_loan(name: str) -> None:
    """Only explicit loan words count: "gave 200 to the watchman" is still an expense, and a
    refund, change from a shop or "I'll be back" are no loans."""
    assert [case["text"] for case in cases(name) if read(case["text"])] == []


def test_each_part_of_a_sentence_is_its_own_loan() -> None:
    assert said("lent Ravi 500 and borrowed 200 from Meena") == [
        ("new", "lent", "Ravi", "500"),
        ("new", "borrowed", "Meena", "200"),
    ]
    # A part that says no loan word is the same kind of loan as its neighbour.
    assert said("Ravi ko 500 aur Priya ko 300 udhaar diye") == [
        ("new", "lent", "Ravi", "500"),
        ("new", "lent", "Priya", "300"),
    ]


def test_the_direction_is_marked_when_the_words_do_not_say_who_gave() -> None:
    (named,) = read("Ravi paid back 500")  # to the user, most likely
    (mine,) = read("I repaid Anand 300")
    (theirs,) = read("ரவி 500 திருப்பிக் கொடுத்தான்")  # the ending says who

    assert named.confidence["direction"] < LEXICON.uncertain_below
    assert mine.confidence["direction"] >= LEXICON.uncertain_below
    assert theirs.confidence["direction"] >= LEXICON.uncertain_below


def test_from_anywhere_only_new_loans_count() -> None:
    """Outside the Expenses page "back" is seldom about money; a new loan still is one."""
    assert read("Ravi paid back 500", repayments=False) == []
    assert [d.action for d in read("lent Ravi 500", repayments=False)] == ["new"]


def test_the_day_is_read_and_nothing_is_made_up() -> None:
    (dated,) = read("I lent 2000 to Priya yesterday")
    (vague,) = read("lent Ravi some money")
    bank = read("Received Rs.350.00 from Meena via UPI on 04-Oct-26, paid back. Ref no 1122")

    assert (dated.date, dated.counterparty) == (date(2026, 10, 7), "Priya")
    assert (vague.amount, vague.counterparty, vague.confidence["amount"]) == (None, "Ravi", 0.0)
    # The name is picked out of the sentence: always for the user to check.
    assert dated.confidence["counterparty"] < LEXICON.uncertain_below
    assert bank == []  # a bank message is one entry, whatever it says
    assert read("gave Ravi 500") == []
