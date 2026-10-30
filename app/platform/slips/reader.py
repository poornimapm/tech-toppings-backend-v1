"""A printed slip (a shop's receipt, a bill, an invoice) as a phone camera and OCR read it: its
lines and figures, the shop's name, the day, the item lines, the totals and what was added on
top. What a module makes of it (draft expenses, a warranty record) is the module's.

The words are data (``words.yaml`` beside this file). English words match whole; Tamil and Hindi
ones at a word's start, as stems, unless short (``word_pattern``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.core.daterange import on_or_before
from app.platform.agents.lexicon import normalise, western_digits

WORDS = Path(__file__).with_name("words.yaml")
# "2x25.00" and "2 X 25.00" as separate tokens: a count, the sign, a price.
TIMES = re.compile("(?<=\\d)\\s*[xX\N{MULTIPLICATION SIGN}*@]\\s*(?=\\d)")
TIMES_SIGNS = {"x", "\N{MULTIPLICATION SIGN}", "*", "@"}
# A figure as printed: 1250, 1,250.00 or 1,23,456.50, a decimal comma (12,50), or a weight in
# kg with grams (1.500).
FIGURE = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)(?:\.(\d{1,2})|,(\d{2})|\.(\d{3}))?"
)
PIN = re.compile(r"\d{6}")  # a postal code ends an address line; a price has paise or commas
SERIAL = re.compile(r"^\d{1,3}\s*[.)\]:-]?\s+(?=\D)")  # "1. Tomato", "01) Tomato"
CODE = re.compile(r"(?<!\S)\d{5,}(?!\S)")  # a barcode or HSN code inside a title
PRICE = re.compile(r"\d[.,]\d{2}$")  # a figure printed with its paise
SPECK = re.compile(r"[A-Za-z]{1,3}")
WORDY = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
# Letters that can follow inside a Tamil or Hindi word (its vowel signs aren't \w).
INDIC = "ऀ-ॿ஀-௿"
EDGES = " .:;,-*#=|_~'\""
BRACKETS = {"(": ")", "[": "]", "{": "}"}
# A rate on a line: a tax or charge ("CGST 2.5%"), even where the camera garbled its name.
PERCENT = re.compile(r"(?<![\d.])\d{1,2}(?:\.\d{1,2})?\s*%(?!\w)")
SHORT_WORD = 3  # a Tamil or Hindi word this short matches only whole
MIN_LETTERS = 2
NAME_LENGTH = 80


def word_pattern(items: Iterable[Any]) -> re.Pattern[str]:
    """One pattern for a list of words, matched in normalised text: English words whole; Tamil
    and Hindi ones at a word's start, as stems, unless short (कुल is not कुल्फी)."""
    parts = []
    for word in sorted({normalise(str(item)) for item in items}, key=len, reverse=True):
        escaped = re.escape(word)
        if word.isascii():
            parts.append(rf"(?<!\w){escaped}(?!\w)")
        else:
            end = rf"(?![\w{INDIC}])" if len(word) <= SHORT_WORD else ""
            parts.append(rf"(?<![\w{INDIC}]){escaped}{end}")
    return re.compile("|".join(parts) or "(?!)")


@dataclass(frozen=True)
class SlipWords:
    headers: re.Pattern[str]
    total: re.Pattern[str]
    not_total: re.Pattern[str]
    payment: re.Pattern[str]
    added: re.Pattern[str]  # charges and taxes
    skipped: re.Pattern[str]  # discounts, not_items and headers
    labels: re.Pattern[str]
    dates: tuple[tuple[re.Pattern[str], str], ...]
    months: Mapping[str, int]  # a month's name -> 1 (January) .. 12
    ocr_digits: Mapping[int, str]
    currency: re.Pattern[str]  # currency marks before or after a figure


@cache
def load_slip_words(path: Path = WORDS) -> SlipWords:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    marks = "|".join(map(re.escape, sorted(map(str, data["currency"]), key=len, reverse=True)))
    return SlipWords(
        headers=word_pattern(data["headers"]),
        total=word_pattern(data["total"]),
        not_total=word_pattern(data["not_total"]),
        payment=word_pattern(data["payment"]),
        added=word_pattern(data["charges"] + data["taxes"]),
        skipped=word_pattern(data["discount"] + data["not_items"] + data["headers"]),
        labels=word_pattern(data["labels"]),
        dates=tuple((re.compile(item["pattern"]), item["order"]) for item in data["dates"]),
        months={
            normalise(str(name)): number
            for number, names in enumerate(data["months"], start=1)
            for name in names
        },
        ocr_digits=str.maketrans(
            {str(key): str(value) for key, value in data["ocr_digits"].items()}
        ),
        currency=re.compile(rf"^(?:{marks})+|(?:{marks})+$", re.IGNORECASE),
    )


@dataclass
class SlipFigure:
    value: Decimal
    raw: str  # as printed
    paise: bool  # printed with paise (40.00), so a price, not a count
    weight: bool  # kg with grams (1.500)


@dataclass
class SlipLine:
    text: str  # as printed, with Western digits
    plain: str  # normalised, for matching words
    figures: list[SlipFigure] = field(default_factory=list)  # the figures it ends with, in order
    title: str = ""  # what comes before them

    @property
    def amount(self) -> Decimal:
        return self.figures[-1].value if self.figures else Decimal(0)


@dataclass
class Slip:
    lines: list[SlipLine]
    items: list[tuple[SlipLine, str]]  # the item lines, with their names
    totals: list[Decimal]  # every total line's amount, in order
    added: list[tuple[str, Decimal]]  # charges and taxes: (label, amount)
    day: date | None
    merchant: str | None  # the shop's name as printed, tidied

    @property
    def total(self) -> Decimal | None:
        """The amount paid: the last total ("Grand Total" before "Net Payable")."""
        return self.totals[-1] if self.totals else None


def letters(text: str) -> int:
    """Letters, a Tamil or Hindi vowel sign counting as one (டீ, घी are two)."""
    return sum(unicodedata.category(letter)[0] in "LM" for letter in text)


def _figure(token: str, words: SlipWords) -> SlipFigure | None:
    """A token as a figure: currency marks off, look-alike letters read as digits."""
    raw = words.currency.sub("", token)
    digits = sum(letter.isdigit() for letter in raw)
    if not digits or digits * 2 < len(raw.replace(".", "").replace(",", "")):
        return None  # a word with a digit in it ("5kg", "B12"), not a figure
    # Look-alike letters only in a price with its paise ("8O.0O"): "15s" is a strip of 15.
    found = FIGURE.fullmatch(raw.translate(words.ocr_digits) if re.search(r"[.,]", raw) else raw)
    if found is None:
        return None
    whole, point, comma, grams = found.groups()
    fraction = point or comma or grams or ""
    value = Decimal(f"{whole.replace(',', '')}.{fraction or '0'}")
    return SlipFigure(value, raw, bool(point or comma), bool(grams))


def _read_line(text: str, words: SlipWords) -> SlipLine:
    # Marks with no letter or digit at either end are the paper's edge or a speck ("|", "!").
    tokens = TIMES.sub(" x ", text).split()
    while tokens and not any(letter.isalnum() for letter in tokens[0]):
        tokens.pop(0)
    figures: list[SlipFigure] = []
    if len(tokens) > 1 and SPECK.fullmatch(tokens[-1]) and PRICE.search(tokens[-2]):
        tokens.pop()  # "770.00 eel": a short scrap of letters after a price is the edge, misread
    while tokens:
        last = tokens[-1]
        sign = last.casefold() in TIMES_SIGNS and figures
        if sign or words.currency.fullmatch(last) or not any(c.isalnum() for c in last):
            tokens.pop()  # "2 x 25.00", "Rs 60", "INR 714.00", "80.00 |"
            continue
        figure = _figure(last, words)
        if figure is None or (figure.weight and not figures):
            break  # a weight comes before a price, never last
        figures.insert(0, figure)
        tokens.pop()
    if len(figures) > 1 and figures[-2].paise and not figures[-1].paise:
        figures.pop()  # "40.00 4": prices have paise, so a bare figure after them is a speck
    title = CODE.sub(" ", SERIAL.sub("", " ".join(tokens)))
    return SlipLine(text, normalise(text), figures, " ".join(title.split()).strip(EDGES))


def _month(name: str, months: Mapping[str, int]) -> int | None:
    """A month by name ("oct"), a Tamil or Hindi one also with an ending attached."""
    plain = normalise(name)
    if plain in months:
        return months[plain]
    return next(
        (
            number
            for word, number in months.items()
            if not word.isascii() and len(word) >= SHORT_WORD and plain.startswith(word)
        ),
        None,
    )


def slip_date(text: str, today: date, words: SlipWords, max_age_days: int) -> date | None:
    """The first date printed on a line, read day first; never after today, nor older than
    ``max_age_days`` (more likely a misread)."""
    for pattern, order in words.dates:
        for found in pattern.finditer(text):
            parts = dict(zip(order, found.groups(), strict=True))
            month = parts["m"]
            number = int(month) if month.isdigit() else _month(month, words.months)
            when = on_or_before(today, int(parts["d"]), number, int(parts["y"])) if number else None
            if when and today - timedelta(days=max_age_days) <= when <= today:
                return when
    return None


def tidy_name(text: str) -> str:
    """A name as printed, without OCR's edges and gaps, and capitalised if it shouts."""
    name = " ".join(text.split()).strip(EDGES)
    while name and unicodedata.category(name[0]).startswith("M"):  # a stray vowel sign
        name = name[1:].strip(EDGES)
    for opening, closing in BRACKETS.items():  # a bracket with no partner is the paper's edge
        if name.endswith(opening) or (name.endswith(closing) and opening not in name):
            name = name[:-1].strip(EDGES)
        if name.startswith(closing) or (name.startswith(opening) and closing not in name):
            name = name[1:].strip(EDGES)
    if name.isascii() and name.isupper():
        name = WORDY.sub(lambda word: word.group(0).capitalize(), name)
    return name[:NAME_LENGTH]


def _merchant(lines: list[SlipLine], words: SlipWords) -> str | None:
    """The shop's name: the first line at the top that names something, before any figures."""
    for line in lines:
        if line.figures and not PIN.fullmatch(line.figures[-1].raw):
            return None
        if letters(line.text) <= MIN_LETTERS or words.skipped.search(line.plain):
            continue
        return tidy_name(line.text) or None
    return None


def _item_name(line: SlipLine, words: SlipWords) -> str | None:
    """An item's name without a column label after it ("Sugar 1kg MRP"); None for a line that
    is only labels, or has too few letters to name anything."""
    plain = normalise(line.title)
    label = words.labels.search(plain)
    title = line.title
    if label is not None:
        cut = len(" ".join(line.title.split()[: len(plain[: label.start()].split())]))
        title = line.title[:cut].strip(EDGES)
    return title if letters(title) >= MIN_LETTERS else None


def read_slip(
    text: str,
    today: date,
    *,
    max_age_days: int,
    max_amount: Decimal,
    words: SlipWords | None = None,
) -> Slip:
    """``max_age_days``: the oldest a printed date may be. ``max_amount``: figures above this
    aren't prices (phone numbers, bill numbers)."""
    words = words or load_slip_words()
    lines = [
        _read_line(western_digits(raw).strip(), words) for raw in text.splitlines() if raw.strip()
    ]
    items: list[tuple[SlipLine, str]] = []
    totals: list[Decimal] = []
    added: list[tuple[str, Decimal]] = []
    day: date | None = None
    for line in lines:
        dated = slip_date(line.text, today, words, max_age_days)
        day = day or dated
        if not 0 < line.amount <= max_amount or PIN.fullmatch(line.figures[-1].raw):
            continue  # nothing to pay, a phone number, or a postal code
        if dated or words.payment.search(line.plain):
            continue  # "12 Oct 2026", and how it was paid ("Cash Received Amount 200.00")
        if words.total.search(line.plain):
            if not words.not_total.search(line.plain):
                totals.append(line.amount)
            continue  # subtotals and "total qty" are never items
        if words.added.search(line.plain):
            added.append((line.title, line.amount))
        elif words.skipped.search(line.plain):
            continue  # a discount, a subtotal, a code, a contact
        elif PERCENT.search(line.title):
            added.append((line.title, line.amount))
        elif name := _item_name(line, words):
            items.append((line, name))
    return Slip(lines, items, totals, added, day, _merchant(lines, words))
