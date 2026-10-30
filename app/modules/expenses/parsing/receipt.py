"""A shop receipt's text, as a phone camera and OCR read it, as draft entries: one per item
bought, with the shop and the day, plus what makes them add up to the total paid.

The platform reads the slip (``read_slip``: its item lines, totals, shop and day). What expenses
makes of it is data too: the scan-receipt skill's ``references/receipt.yaml``, and the expenses
words it shares (categories, units, confidence) from the track-expenses skill's lexicon.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.modules.expenses.parsing.lexicon import Lexicon, lookup
from app.modules.expenses.parsing.sentence import EXPENSE, Draft, category
from app.platform import SlipFigure, SlipLine, normalise, read_slip, word_pattern

SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([^\d\s]+)")  # "500 g", "1kg", "1 கிலோ" in a title
MAX_COUNT = 99  # a count column above this is more likely a misread price
KG = "kg"
TITLE_LENGTH = 120


@dataclass(frozen=True)
class ReceiptWords:
    tool: str
    shops: Mapping[str, re.Pattern[str]]
    max_age_days: int
    max_amount: Decimal
    round_off: Decimal
    titles: Mapping[str, Mapping[str, str]]
    confidence: Mapping[str, float]


@cache
def load_receipt(folder: Path) -> ReceiptWords:
    data: dict[str, Any] = yaml.safe_load((folder / "receipt.yaml").read_text(encoding="utf-8"))
    return ReceiptWords(
        tool=data["tool"],
        shops={key: word_pattern(group) for key, group in data["shops"].items()},
        max_age_days=int(data["max_age_days"]),
        max_amount=Decimal(str(data["max_amount"])),
        round_off=Decimal(str(data["round_off"])),
        titles=data["titles"],
        confidence={name: float(value) for name, value in data["confidence"].items()},
    )


@dataclass
class Receipt:
    drafts: list[Draft]
    total: Decimal | None  # the amount paid, when the receipt shows one


def _shop(merchant: str | None, words: ReceiptWords) -> str | None:
    plain = normalise(merchant or "")
    return next((key for key, names in words.shops.items() if names.search(plain)), None)


def _count(line: SlipLine) -> SlipFigure | None:
    """The count (or weight) column before the price, if the line has one."""
    figures, amount = line.figures, line.amount
    if len(figures) >= 3:  # noqa: PLR2004 - count, price, amount
        times, price = figures[-3], figures[-2].value
        close = abs(times.value * price - amount) <= max(Decimal("0.05"), amount / 50)
        return times if close else None
    if len(figures) == 2:  # noqa: PLR2004 - a count or a price, then the amount
        first = figures[0]
        if not first.paise and not first.weight and 0 < first.value <= MAX_COUNT:
            return first if first.value < amount else None
        if first.paise and first.value:
            ratio = amount / first.value
            if ratio == ratio.to_integral() and 1 < ratio <= MAX_COUNT:
                return SlipFigure(ratio, str(ratio), paise=False, weight=False)
    return None


def _quantity(line: SlipLine, lexicon: Lexicon) -> tuple[Decimal | None, str | None]:
    """How much was bought: a size in the title ("1 kg") times the count, a weight column
    (1.500 kg), or the count of pieces."""
    count = _count(line)
    size = next(
        (
            (Decimal(found.group(1)), unit)
            for found in SIZE.finditer(normalise(line.title))
            if (unit := lookup(found.group(2), lexicon.units))
        ),
        None,
    )
    if count is not None and count.weight:
        return count.value, KG
    if size:
        return size[0] * (count.value if count else 1), size[1]
    if count is not None and count.value == count.value.to_integral() and count.value > 1:
        return count.value, "piece"
    return None, None


@dataclass(frozen=True)
class _Maker:
    """Drafts for one receipt: its day and shop, and how to file and name what it holds."""

    lexicon: Lexicon
    words: ReceiptWords
    locale: str
    day: date | None
    merchant: str | None
    habits: Mapping[str, str]
    usable: Set[str]

    def named(self, name: str) -> str:
        """A title the receipt doesn't print (``titles`` in receipt.yaml), in their language."""
        titles = self.words.titles[name]
        return titles.get(self.locale) or next(iter(titles.values()))

    def draft(
        self, title: str, amount: Decimal, *, adjusted: bool = False, shop_first: bool = False
    ) -> Draft:
        """``shop_first``: the draft is the whole receipt, so the kind of shop says what it was
        ("Indian Oil" is fuel, whatever "oil" says)."""
        sure = self.words.confidence
        shop = _shop(self.merchant, self.words)
        guess = (shop, sure["shop_category"]) if shop else None
        filed: str | None
        if shop_first and guess and shop in self.usable:
            filed, filed_sure = guess
        else:
            filed, filed_sure = category(
                normalise(title), EXPENSE, self.lexicon, self.habits, self.usable, guess
            )
        read = sure["adjusted" if adjusted else "item"]
        return Draft(
            kind=EXPENSE,
            title=title[:TITLE_LENGTH],
            amount=amount,
            category_id=filed,
            date=self.day,
            merchant=self.merchant,
            confidence={
                "amount": read,
                "title": read,
                "category_id": filed_sure if filed else 0.0,
                "date": sure["date_read"] if self.day else sure["date_missing"],
            },
        )


def read_receipt(  # noqa: PLR0913 - the text, its day and language, and what to file it under
    text: str,
    today: date,
    lexicon: Lexicon,
    words: ReceiptWords,
    *,
    locale: str,
    max_drafts: int,
    habits: Mapping[str, str] | None = None,
    categories: Mapping[str, Set[str]] | None = None,
) -> Receipt:
    """``max_drafts``: the most the drafts tool takes. ``habits``: the user's usual category by
    (normalised) title. ``categories``: the ids they may use, by kind."""
    slip = read_slip(text, today, max_age_days=words.max_age_days, max_amount=words.max_amount)
    maker = _Maker(
        lexicon,
        words,
        locale,
        slip.day,
        slip.merchant,
        habits or {},
        (categories or {}).get(EXPENSE, frozenset()),
    )
    keep = max_drafts - 2  # room for the rest of a long receipt, and for what balances it
    drafts = []
    for line, title in slip.items[:keep]:
        made = maker.draft(title, line.amount)
        made.quantity, made.unit = _quantity(line, lexicon)
        drafts.append(made)
    if rest := sum((line.amount for line, _ in slip.items[keep:]), Decimal(0)):
        drafts.append(maker.draft(maker.named("rest"), rest, adjusted=True))
    total = slip.total
    if total is not None and not drafts:
        title = maker.merchant or maker.named("receipt")
        drafts = [maker.draft(title, total, adjusted=maker.merchant is None, shop_first=True)]
    elif total is not None:
        _balance(drafts, total, slip.added, maker)
    return Receipt(drafts, total)


def _balance(
    drafts: list[Draft], total: Decimal, added: list[tuple[str, Decimal]], maker: _Maker
) -> None:
    """Make the drafts add up to the total paid. Prices on Indian receipts usually include tax,
    so a tax summary below the items is not added: only a total above the items says something
    was. A total below them is a discount, taken off the largest item for the user to check."""
    words = maker.words
    difference = total - sum((made.amount or Decimal(0) for made in drafts), Decimal(0))
    if abs(difference) <= words.round_off:
        return
    adjusted = words.confidence["adjusted"]
    if difference < 0:
        largest = max(drafts, key=lambda made: made.amount or Decimal(0))
        if largest.amount is not None and -difference < largest.amount:
            largest.amount += difference
            largest.confidence["amount"] = adjusted
        else:  # more than any item: something was misread
            for made in drafts:
                made.confidence["amount"] = adjusted
        return
    explained = sum((amount for _, amount in added), Decimal(0))
    known = abs(explained - difference) <= words.round_off and all(label for label, _ in added)
    title = (
        " + ".join(label for label, _ in added)
        if known
        else maker.named("extras" if added else "rest")
    )
    extra = maker.draft(title, difference, adjusted=not known)
    # Filed with the items: a restaurant's service charge is food, a pharmacy's tax is health.
    extra.category_id = Counter(made.category_id for made in drafts).most_common(1)[0][0]
    drafts.append(extra)
