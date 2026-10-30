"""A bill's text, as a phone reads it, as one receipt to keep (the scan-bill skill's static
mode): the main thing bought, the shop, the day, the total paid, the bill's and the thing's
numbers, and the warranty printed on it.

The platform reads the slip (``read_slip``); a bill's own words are data (the skill's
``references/bill.yaml``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.modules.receipts.warranty import rules
from app.platform import SlipLine, read_slip, word_pattern

# Where a number starts after its label ("Invoice No.: INV/26/0042").
LEADING = " :#.-=|"
TRAILING = ".,;:|)"
NUMBER_LENGTHS = {"bill_number": 40, "serial_number": 60}
COUNT = r"(\d{1,3})\s*-?\s*"


def _units(words: Iterable[Any]) -> str:
    """Unit words after a count ("2 years", "2yrs", "6 மாதம்"): English ones whole, Tamil and
    Hindi ones as stems."""
    parts = sorted({str(word).casefold() for word in words}, key=len, reverse=True)
    return "|".join(re.escape(part) + (r"(?!\w)" if part.isascii() else "") for part in parts)


@dataclass(frozen=True)
class BillWords:
    tool: str
    bill_number: re.Pattern[str]
    serial_number: re.Pattern[str]
    warranty: re.Pattern[str]
    period: re.Pattern[str]  # a count of months or years: group 1 the count, then which
    max_age_days: int
    max_amount: Decimal


@cache
def load_bill(folder: Path) -> BillWords:
    data: dict[str, Any] = yaml.safe_load((folder / "bill.yaml").read_text(encoding="utf-8"))
    months, years = _units(data["months"]), _units(data["years"])
    return BillWords(
        tool=data["tool"],
        bill_number=word_pattern(data["bill_number"]),
        serial_number=word_pattern(data["serial_number"]),
        warranty=word_pattern(data["warranty"]),
        period=re.compile(rf"(?<!\d){COUNT}(?:(?P<months>{months})|(?P<years>{years}))"),
        max_age_days=int(data["max_age_days"]),
        max_amount=Decimal(str(data["max_amount"])),
    )


@dataclass
class Bill:
    text: str | None = None
    shop: str | None = None
    bought_on: date | None = None
    amount: Decimal | None = None
    bill_number: str | None = None
    serial_number: str | None = None
    warranty_months: int | None = None
    uncertain: list[str] = field(default_factory=list)

    def arguments(self) -> dict[str, Any]:
        """The bill as the ``receipts_draft`` client tool takes it."""
        return {
            "text": self.text,
            "shop": self.shop,
            "bought_on": self.bought_on.isoformat() if self.bought_on else None,
            "amount": float(self.amount) if self.amount is not None else None,
            "bill_number": self.bill_number,
            "serial_number": self.serial_number,
            "warranty_months": self.warranty_months,
            "uncertain": self.uncertain,
        }


def _spaced(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _after(line: SlipLine, label: re.Pattern[str], longest: int) -> str | None:
    """The number printed after a label on the line ("Bill No: 1234" is 1234), as printed."""
    spaced = _spaced(line.text)
    lowered = spaced.casefold()
    found = label.search(lowered)
    if found is None or len(lowered) != len(spaced):
        return None
    tokens = spaced[found.end() :].strip(LEADING).split()
    number = tokens[0].strip(TRAILING) if tokens else ""
    has_digit = any(letter.isdigit() for letter in number)
    return number if has_digit and len(number) <= longest else None


def _number(lines: list[SlipLine], label: re.Pattern[str], longest: int) -> str | None:
    return next((n for line in lines if (n := _after(line, label, longest))), None)


def _warranties(lines: list[SlipLine], words: BillWords) -> list[int]:
    """Every warranty printed, in months, in order: on a line naming one, or the line just
    after it ("Warranty" / "1 Year"). Lengths no warranty has are misreads, and left out."""
    found: list[int] = []
    for index, line in enumerate(lines):
        if not words.warranty.search(line.plain):
            continue
        for text in (line.plain, *(after.plain for after in lines[index + 1 : index + 2])):
            periods = list(words.period.finditer(text))
            found += [int(p.group(1)) * (12 if p.group("years") else 1) for p in periods]
            if periods:
                break
    return [months for months in found if 0 < months <= rules().warranty.max_months]


def read_bill(text: str, today: date, words: BillWords) -> Bill | None:
    """None when nothing on it can be read as a bill."""
    slip = read_slip(text, today, max_age_days=words.max_age_days, max_amount=words.max_amount)
    labelled = (words.bill_number, words.serial_number, words.warranty)
    # "S/N 12345" and "Warranty months 12" end in figures, but are no things bought.
    items = [
        (line, name)
        for line, name in slip.items
        if not any(label.search(line.plain) for label in labelled)
    ]
    bill = Bill(shop=slip.merchant, bought_on=slip.day)
    if items:
        line, name = max(items, key=lambda item: item[0].amount)
        bill.text = name
        if len(items) > 1:
            bill.uncertain.append("text")  # the dearest of several: the user may mean another
        bill.amount = slip.total
        if bill.amount is None:
            bill.amount = line.amount
            bill.uncertain.append("amount")
    elif slip.total is not None:
        bill.amount = slip.total
    bill.bill_number = _number(slip.lines, words.bill_number, NUMBER_LENGTHS["bill_number"])
    bill.serial_number = _number(slip.lines, words.serial_number, NUMBER_LENGTHS["serial_number"])
    warranties = _warranties(slip.lines, words)
    if warranties:
        # The first printed is the thing's own ("1 year, 10 years on the compressor").
        bill.warranty_months = warranties[0]
        if len(set(warranties)) > 1:
            bill.uncertain.append("warranty_months")
    if bill.bought_on is None:
        bill.uncertain.append("bought_on")
    readable = (bill.text, bill.amount, bill.bill_number, bill.serial_number)
    return bill if any(value is not None for value in readable) else None
