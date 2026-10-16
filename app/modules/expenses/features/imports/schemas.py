from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, model_validator

from app.modules.expenses.domain import EntryKind, PaymentMode
from app.modules.expenses.features.imports.rules import ImportField, import_rules


class Signs(StrEnum):
    """How an amount column without a kind tells money spent from money received."""

    EXPENSE = "expense"  # every amount is money spent
    SIGNED = "signed"  # minus is money spent, plus is money received


AMOUNTS = {ImportField.AMOUNT, ImportField.DEBIT, ImportField.CREDIT}


class ColumnMapping(BaseModel):
    """Which column holds what, and how its days and amounts are written. A guess may lack a
    column it couldn't name; the user says which."""

    model_config = ConfigDict(extra="forbid")

    header_row: int = Field(ge=0, description="The row, from 0, that names the columns")
    columns: dict[ImportField, Annotated[int, Field(ge=0)]] = Field(
        description="Field -> its column, from 0"
    )
    date_format: str = Field(description="One of date_formats, as the preview offers them")
    signs: Signs = Signs.EXPENSE


class ImportMapping(ColumnMapping):
    """A mapping the user gives: enough to make entries."""

    @model_validator(mode="after")
    def enough_to_make_entries(self) -> ImportMapping:
        missing = [
            name
            for name, present in (
                ("date", ImportField.DATE in self.columns),
                ("title", ImportField.TITLE in self.columns),
                ("an amount", bool(AMOUNTS & self.columns.keys())),
            )
            if not present
        ]
        if missing:
            raise ValueError(f"say which column holds {', '.join(missing)}")
        if self.date_format not in import_rules().date_formats:
            raise ValueError("choose one of the date formats offered")
        return self


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: Base64Bytes = Field(description="The CSV file, base64-encoded")
    mapping: ImportMapping | None = Field(
        default=None, description="Leave out to have it guessed from the file"
    )


class CommitIn(ImportIn):
    """The file and the mapping the preview showed, and which of its importable rows to add
    now: from ``offset``, at most ``limits.commit_batch`` of them."""

    mapping: ImportMapping
    include_duplicates: bool = Field(
        default=False, description="Also add rows that look saved already"
    )
    offset: int = Field(default=0, ge=0, description="Importable rows done so far")


class CommitOut(BaseModel):
    added: int = Field(description="Entries this request added")
    already: int = Field(description="Rows of this file an earlier import added already")
    done: int = Field(description="Importable rows done, with this request")
    total: int = Field(description="Importable rows in the file")
    next_offset: int | None = Field(description="Send this as offset for the rest; None: done")


class ProblemCode(StrEnum):
    MISSING_DATE = "missing_date"
    BAD_DATE = "bad_date"  # not a day in the date format
    MISSING_TITLE = "missing_title"
    MISSING_AMOUNT = "missing_amount"  # empty, or nothing
    BAD_AMOUNT = "bad_amount"  # not a number
    CURRENCY = "currency"  # not the user's base currency (entries are kept in it)
    INVALID = "invalid"  # anything else an entry can't be


class ImportEntry(BaseModel):
    """A row as it would be saved."""

    date: dt.date
    time: dt.time | None
    kind: EntryKind
    title: str
    amount_minor: int
    currency: str
    category_id: str
    category_guessed: bool = Field(description="The row named no category of the user's")
    quantity: float | None
    unit: str | None
    merchant: str | None
    payment_mode: PaymentMode | None
    for_whom: str | None
    tags: list[str]
    note: str | None


class ImportRow(BaseModel):
    line: int = Field(description="The row's number, as a spreadsheet shows it")
    entry: ImportEntry | None = Field(description="None when a problem stops it")
    problems: list[ProblemCode]
    duplicate: bool = Field(description="An entry like it (day, kind, amount, title) is saved")


class DateChoice(BaseModel):
    format: str
    example: str = Field(description="31 October 2026 written this way")


class PreviewOut(BaseModel):
    fingerprint: str = Field(description="The file's SHA-256: an import of it adds each row once")
    columns: list[str] = Field(description="The header row's cells")
    mapping: ColumnMapping = Field(description="As given, or as guessed")
    date_formats: list[DateChoice]
    rows: list[ImportRow] = Field(description="The first rows, as they would be saved")
    problems: list[ImportRow] = Field(description="The first rows a problem stops")
    unknown_categories: list[str] = Field(
        description="Categories named in the file that aren't the user's (filed under other)"
    )
    total: int
    ready: int = Field(description="Rows the import would add")
    duplicates: int
    with_problems: int
