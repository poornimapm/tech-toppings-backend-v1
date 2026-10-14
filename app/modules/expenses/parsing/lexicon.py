"""The rule parser's vocabulary, read from a skill's ``references/`` folder (YAML data).

Nothing about any language is in the code: adding a word, a unit, a bank's SMS format or a
whole language means editing those files.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

import yaml

from app.platform import normalise

T = TypeVar("T")
# A Tamil or Hindi word this long also matches with an ending attached ("ரூபாய்க்கு").
STEM_LENGTH = 3
# English words longer than this may be plurals.
PLURAL_LENGTH = 3


def _stem(word: str) -> bool:
    return not word.isascii() and len(word) >= STEM_LENGTH


def singular(token: str) -> str:
    """An English plural without its ending ("coffees" -> "coffee"); anything else as it is."""
    plural = token.isascii() and len(token) > PLURAL_LENGTH and token.endswith("s")
    return token[:-1] if plural else token


def known(token: str, words: Collection[str]) -> bool:
    """Is the token one of the words? English: the word or its plural; Tamil and Hindi: also
    the word with an ending."""
    if token in words or singular(token) in words:
        return True
    return any(token.startswith(word) for word in words if _stem(word))


def lookup(token: str, table: Mapping[str, T]) -> T | None:
    """The table's value for the token, matching words the way ``known`` does."""
    for form in (token, singular(token)):
        if form in table:
            return table[form]
    return next(
        (value for word, value in table.items() if _stem(word) and token.startswith(word)), None
    )


def _words(items: Iterable[Any]) -> frozenset[str]:
    return frozenset(normalise(str(item)) for item in items)


def _ranked(groups: Iterable[Iterable[Any]]) -> dict[str, int]:
    """``[[monday, mon], [tuesday]]`` -> ``{monday: 0, mon: 0, tuesday: 1}``."""
    return {normalise(str(word)): rank for rank, group in enumerate(groups) for word in group}


def _numbers(table: Mapping[Any, Any]) -> dict[str, Decimal]:
    return {normalise(str(word)): Decimal(str(value)) for word, value in table.items()}


@dataclass(frozen=True)
class Sms:
    min_hits: int
    detect: frozenset[str]
    amount: tuple[re.Pattern[str], ...]
    credit: frozenset[str]
    debit: frozenset[str]
    merchant: tuple[re.Pattern[str], ...]
    date: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class Lexicon:
    values: Mapping[str, Decimal]
    multipliers: Mapping[str, Decimal]
    shifts: Mapping[str, Decimal]
    currency: frozenset[str]
    conjunctions: frozenset[str]
    income: frozenset[str]
    spend: frozenset[str]
    fillers: frozenset[str]
    units: Mapping[str, str]  # word -> unit code (data/units.yaml)
    places: frozenset[str]
    last: frozenset[str]
    days: Mapping[str, int]  # word -> how many days ago
    weekdays: Mapping[str, int]  # word -> 0 (Monday) .. 6
    months: Mapping[str, int]  # word -> 0 (January) .. 11
    ago_days: frozenset[str]
    ago_marks: frozenset[str]
    keywords: Mapping[str, frozenset[str]]  # category id -> words
    defaults: Mapping[str, str]  # kind -> category id
    sms: Sms
    confidence: Mapping[str, float]
    uncertain_below: float

    @property
    def phrases(self) -> list[str]:
        """Entries of several words, longest first: matched as a whole before splitting."""
        every = [
            self.currency, self.conjunctions, self.income, self.spend, self.fillers, self.units,
            self.days, self.ago_days, self.ago_marks, self.last,
        ]  # fmt: skip
        found = {word for group in every for word in group if " " in word}
        return sorted(found, key=len, reverse=True)


def _read(folder: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((folder / f"{name}.yaml").read_text(encoding="utf-8"))
    return data


def _patterns(items: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(item, re.IGNORECASE) for item in items)


@cache
def load(folder: Path) -> Lexicon:
    numbers, words = _read(folder, "numbers"), _read(folder, "words")
    categories, sms, skill = (
        _read(folder, "categories"),
        _read(folder, "sms"),
        _read(folder, "lexicon"),
    )
    capture = skill["capture"]
    return Lexicon(
        values=_numbers(numbers["values"]),
        multipliers=_numbers(numbers["multipliers"]),
        shifts=_numbers(numbers["shifts"]),
        currency=_words(words["currency"]),
        conjunctions=_words(words["conjunctions"]),
        income=_words(words["income"]),
        spend=_words(words["spend"]),
        fillers=_words(words["fillers"]),
        units={normalise(word): code for code, group in words["units"].items() for word in group},
        places=_words(words["places"]),
        last=_words(words["last"]),
        days={normalise(word): int(ago) for ago, group in words["days"].items() for word in group},
        weekdays=_ranked(words["weekdays"]),
        months=_ranked(words["months"]),
        ago_days=_words(words["ago"]["days"]),
        ago_marks=_words(words["ago"]["marks"]),
        keywords={key: _words(group) for key, group in categories["keywords"].items()},
        defaults=dict(categories["defaults"]),
        sms=Sms(
            min_hits=int(sms["min_hits"]),
            detect=_words(sms["detect"]),
            amount=_patterns(sms["amount"]),
            credit=_words(sms["credit"]),
            debit=_words(sms["debit"]),
            merchant=_patterns(sms["merchant"]),
            date=_patterns(sms["date"]),
        ),
        confidence={name: float(value) for name, value in capture["confidence"].items()},
        uncertain_below=float(capture["uncertain_below"]),
    )
