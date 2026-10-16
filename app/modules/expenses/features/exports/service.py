"""The user's entries as a CSV file, with the entries list's own filters, so a download holds
exactly what the list shows. Streamed in batches, so memory stays small however long the file.

The columns are the entry's fields under stable English names (the import reads them back);
amounts are written in the currency's main unit with every decimal ("250.50"). Text that a
spreadsheet would run as a formula is written so it can't be."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.schemas import EntryFilters
from app.modules.expenses.listing import listing
from app.platform import ActorContext, AppError, major_text

COLUMNS = (
    "date",
    "time",
    "kind",
    "title",
    "amount",
    "currency",
    "category",
    "quantity",
    "unit",
    "merchant",
    "payment_mode",
    "for_whom",
    "tags",
    "note",
)
TAG_SEPARATOR = ";"
BATCH = 500  # rows written before the text so far is sent
# A cell starting with one of these is a formula to a spreadsheet (OWASP "CSV injection").
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
NEWEST_FIRST = [("date", -1), ("created_at", -1), ("_id", -1)]


class ExportTooLargeError(AppError):
    status_code = 422
    code: ClassVar[str] = "EXPENSE_EXPORT_TOO_LARGE"
    default_message = "That's more entries than one file can take. Choose a shorter period."


def safe_text(value: str | None) -> str:
    """Text a spreadsheet would run as a formula, written so it can't be."""
    if not value:
        return ""
    return f"'{value}" if value.startswith(FORMULA_START) else value


def _row(entry: dict[str, Any], categories: dict[str, str]) -> list[str]:
    quantity = entry.get("quantity")
    return [
        entry["date"],
        entry.get("time") or "",
        entry["kind"],
        safe_text(entry["title"]),
        major_text(entry["amount_minor"], entry["currency"]),
        entry["currency"],
        safe_text(categories.get(entry["category_id"], entry["category_id"])),
        "" if quantity is None else f"{quantity:g}",
        entry.get("unit") or "",
        safe_text(entry.get("merchant")),
        entry.get("payment_mode") or "",
        safe_text(entry.get("for_whom")),
        safe_text(TAG_SEPARATOR.join(entry.get("tags") or [])),
        safe_text(entry.get("note")),
    ]


class Exports:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.entries = EntryRepository(ctx.scope, ctx.clock)

    def _criteria(self, filters: EntryFilters) -> dict[str, Any]:
        return self.entries.query(filters.criteria(), include_deleted=filters.deleted)

    async def check(self, filters: EntryFilters) -> None:
        """Before anything is sent: a file too long is refused while it can still be said."""
        most = listing().export.max_rows
        if await Entry.find(self._criteria(filters)).count() > most:
            raise ExportTooLargeError(details={"max": most})

    def filename(self, filters: EntryFilters) -> str:
        days = [day.isoformat() for day in (filters.start, filters.end) if day]
        return f"expenses-{'-to-'.join(days) or self.ctx.today().isoformat()}.csv"

    async def lines(self, filters: EntryFilters) -> AsyncIterator[str]:
        """The file: a byte-order mark (spreadsheets then read Tamil and Hindi right), the
        header, then the entries newest first, in batches."""
        # A category is named as the user knows it: their own by its name, a system one by its key.
        categories = {
            item.id: item.name or item.key or item.id
            for item in await Categories(self.ctx.scope, self.ctx.clock).all()
        }
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        buffer.write("﻿")
        writer.writerow(COLUMNS)
        cursor = Entry.get_pymongo_collection().find(self._criteria(filters)).sort(NEWEST_FIRST)
        written = 0
        async for entry in cursor.batch_size(BATCH):
            writer.writerow(_row(entry, categories))
            written += 1
            if written % BATCH == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()
