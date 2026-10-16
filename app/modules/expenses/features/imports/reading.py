"""Reading a CSV file: its text, its rows, which column is which, and each cell as a value. No
database here: the preview and the import read a file the same way."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import unicodedata
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.imports.rules import ImportField, ImportRules
from app.platform import AppError, normalise

DELIMITERS = ",;\t|"
SNIFF_CHARS = 4096
# An amount once currency words and symbols are gone: a sign or brackets, digits with grouping
# commas, a dot.
NUMBER = re.compile(r"^(?P<minus>[-(]?)\+?(?P<digits>[0-9][0-9,]*(\.[0-9]+)?)\)?$")
CURRENCY_WORDS = re.compile(r"[A-Za-z]+\.?")  # "Rs.", "INR"
# Marks a header may carry that don't change its name ("Withdrawal Amt.", "Date:").
HEADER_MARKS = re.compile(r"[.:#*()\[\]]")


class ImportUnreadableError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_IMPORT_UNREADABLE"
    default_message = "That file can't be read as a CSV file."


class ImportTooLargeError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_IMPORT_TOO_LARGE"
    default_message = "That file is longer than one import can take. Split it by period."


def decode(data: bytes) -> str:
    """UTF-8 (with or without a byte-order mark), else Windows' Western encoding, as a
    spreadsheet saves it."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


Row = tuple[int, list[str]]  # its number as a spreadsheet shows it, and its cells


def table(text: str) -> list[Row]:
    """The file's rows, cells trimmed, rows with nothing in them left out."""
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:SNIFF_CHARS], delimiters=DELIMITERS
        )
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [
            (number, [cell.strip() for cell in cells])
            for number, cells in enumerate(csv.reader(io.StringIO(text), dialect), start=1)
        ]
    except csv.Error as exc:
        raise ImportUnreadableError from exc
    return [(number, cells) for number, cells in rows if any(cells)]


def _name(cell: str) -> str:
    """A header as compared: any case and spacing, without trailing dots and such."""
    return normalise(HEADER_MARKS.sub("", cell))


def _words(rules: ImportRules) -> dict[str, ImportField]:
    return {_name(word): field for field, words in rules.columns.items() for word in words}


def header_row(rows: Sequence[Row], rules: ImportRules) -> int:
    """The row that names the columns, as a place in ``rows``: of the first few, the one most
    cells of which are known column names (a bank statement starts with lines about the
    account)."""
    words = _words(rules)
    scores = [
        sum(_name(cell) in words for cell in cells)
        for _, cells in rows[: rules.limits.header_search_rows]
    ]
    best = max(range(len(scores)), key=lambda index: scores[index], default=0)
    return best if scores and scores[best] >= 2 else 0  # noqa: PLR2004 - two names make a header


def guess_columns(header: Sequence[str], rules: ImportRules) -> dict[ImportField, int]:
    """Each field the header names, with its column; the first column for a field wins."""
    words = _words(rules)
    found: dict[ImportField, int] = {}
    for index, cell in enumerate(header):
        field = words.get(_name(cell))
        if field is not None and field not in found:
            found[field] = index
    return found


def parse_day(text: str, day_format: str) -> dt.date | None:
    """The day, or None when there is none; ValueError when it isn't one in this format."""
    return dt.datetime.strptime(text, day_format).date() if text else None  # noqa: DTZ007


def guess_date_format(values: Iterable[str], rules: ImportRules) -> str:
    """The first format that reads every day given, else the one that reads the most."""
    days = [value for value in values if value]

    def read(day_format: str) -> int:
        count = 0
        for value in days:
            try:
                parse_day(value, day_format)
            except ValueError:
                continue
            count += 1
        return count

    counts = {day_format: read(day_format) for day_format in rules.date_formats}
    every = [day_format for day_format, count in counts.items() if count == len(days)]
    return every[0] if every else max(counts, key=lambda day_format: counts[day_format])


def parse_time(text: str, rules: ImportRules) -> dt.time | None:
    """The time of day, or None when there is none or it can't be read (it is only a detail)."""
    for time_format in rules.time_formats:
        try:
            return dt.datetime.strptime(text.upper(), time_format).time()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def kind_of(text: str, rules: ImportRules) -> EntryKind | None:
    word = normalise(text)
    for kind, words in rules.kinds.items():
        if word in {normalise(item) for item in words}:
            return kind
    return None


def parse_amount(text: str, rules: ImportRules) -> tuple[Decimal, EntryKind | None] | None:
    """``"₹1,23,456.50"`` -> ``(123456.50, None)``; ``"(500)"`` or ``"-500"`` is negative;
    ``"500 Dr"`` says its kind. None when the cell is empty; ValueError when it isn't a number."""
    raw = text.strip()
    if not raw:
        return None
    kind = None
    words = raw.split()
    if len(words) > 1 and (said := kind_of(words[-1], rules)):
        kind, raw = said, " ".join(words[:-1])
    kept = "".join(
        char
        for char in CURRENCY_WORDS.sub("", raw)
        if not char.isspace() and unicodedata.category(char) != "Sc"  # ₹, $ ...
    )
    found = NUMBER.fullmatch(kept)
    if found is None:
        raise ValueError(f"not an amount: {text!r}")
    try:
        value = Decimal(found["digits"].replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - NUMBER admits only valid decimals
        raise ValueError(f"not an amount: {text!r}") from exc
    return (-value if found["minus"] else value), kind
