"""Numbers as people say and type them: digits in any script, number words, "2k", "ढाई सौ"."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.modules.expenses.parsing.lexicon import Lexicon

DIGITS = re.compile(r"^\d+(?:\.\d+)?$")
SHORT_THOUSANDS = re.compile(r"^(\d+(?:\.\d+)?)k$")
TEN, TWENTY, HUNDRED = Decimal(10), Decimal(20), Decimal(100)


@dataclass(frozen=True)
class Word:
    raw: str  # as typed, for titles
    text: str  # normalised, for matching


@dataclass(frozen=True)
class Number:
    raw: str
    value: Decimal


Item = Word | Number


def _run(words: list[Word], start: int, lexicon: Lexicon) -> tuple[int, Decimal | None]:
    """The number starting at ``start``: where it ends and its value (or ``None``)."""
    total = current = Decimal(0)
    shift: Decimal | None = None
    seen = spelled = False
    last_unit = False  # the previous word was a single digit word: "two" in "two fifty"
    end = start
    while end < len(words):
        text = words[end].text
        if not seen and (short := SHORT_THOUSANDS.match(text)):
            return end + 1, Decimal(short[1]) * 1000
        if DIGITS.match(text):
            if seen:
                break  # a second figure is a second number
            current, shift, seen = Decimal(text) + (shift or 0), None, True
        elif text in lexicon.shifts and not seen and shift is None:
            shift = lexicon.shifts[text]
        elif text in lexicon.values and (spelled or not seen):
            value = lexicon.values[text] + (shift or 0)
            tens = TWENTY <= value < HUNDRED and value % TEN == 0
            # "two fifty" is 250, not 52.
            current = current * 100 + value if last_unit and tens else current + value
            last_unit = not spelled and 0 < value < TEN and value % 1 == 0
            shift, seen, spelled = None, True, True
        elif text in lexicon.multipliers and (seen or len(text) > 1):
            factor = lexicon.multipliers[text]
            if factor == HUNDRED:
                current = (current or 1) * factor
            else:
                total, current = total + (current or 1) * factor, Decimal(0)
            seen, spelled, last_unit = True, True, False
        elif (
            text == "and"
            and spelled
            and end + 1 < len(words)
            and words[end + 1].text in lexicon.values
        ):
            pass  # "two hundred and fifty"
        else:
            break
        end += 1
    if shift is not None:  # "सवा" on its own is 1.25
        current, seen = current + 1 + shift, True
    return (end, total + current) if seen else (start, None)


def read(words: list[Word], lexicon: Lexicon) -> list[Item]:
    """The words with every run of number words replaced by one ``Number``."""
    items: list[Item] = []
    position = 0
    while position < len(words):
        end, value = _run(words, position, lexicon)
        if value is None:
            items.append(words[position])
            position += 1
        else:
            items.append(Number(" ".join(word.raw for word in words[position:end]), value))
            position = end
    return items
