"""CSV files the app gives out, written one way (D5, ADR-0024): UTF-8 behind a byte-order mark
(spreadsheets then read Tamil and Hindi right), CRLF line ends, the csv module's quoting, and
text a spreadsheet would run as a formula written so it can't be (OWASP "CSV injection")."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

BOM = "﻿"
# A cell starting with one of these is a formula to a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
# A route's OpenAPI entry for a CSV answer.
CSV_RESPONSE: dict[int | str, dict[str, object]] = {
    200: {"content": {"text/csv": {}}, "description": "UTF-8 CSV with a byte-order mark"}
}


def safe_text(value: str | None) -> str:
    """Text a spreadsheet would run as a formula, written so it can't be. Only for free text
    (names, notes): dates, numbers and fixed words never need it."""
    if not value:
        return ""
    return f"'{value}" if value.startswith(FORMULA_START) else value


def csv_writer(buffer: io.StringIO) -> Any:
    """A writer for one file, after the byte-order mark is written to ``buffer``."""
    buffer.write(BOM)
    return csv.writer(buffer, lineterminator="\r\n")


def csv_text(columns: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """A whole (small) file: the mark, the header, then the rows."""
    buffer = io.StringIO()
    writer = csv_writer(buffer)
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def attachment(name: str) -> dict[str, str]:
    """The header that saves the answer as a file named ``name`` (ASCII: no names of people or
    projects, which may be Tamil or Hindi)."""
    return {"Content-Disposition": f'attachment; filename="{name}"'}
