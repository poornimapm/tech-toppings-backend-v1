"""Importing entries from a CSV file: a bank statement, a spreadsheet, or this app's export.

The server keeps nothing between the preview and the import: each sends the file and its
mapping, and both read it the same way. Each row is made an entry by the same rules as one
typed in (EntryIn), filed under the user's category of that name or, failing that, under
"other"; a row that looks saved already (same day, kind, amount and title) is marked.

The import adds the rows in batches, the app asking for each. Every row is saved once per
file and row number (its client key), so a batch sent again, or the whole file imported again,
adds nothing twice."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.modules.expenses.domain import MODULE, EntryKind, EntrySource, PaymentMode
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.schemas import MAX_TAGS, EntryIn
from app.modules.expenses.features.entries.service import EntryService
from app.modules.expenses.features.imports.reading import (
    ImportTooLargeError,
    Row,
    decode,
    guess_columns,
    guess_date_format,
    header_row,
    kind_of,
    parse_amount,
    parse_day,
    parse_time,
    table,
)
from app.modules.expenses.features.imports.rules import ImportField, ImportRules, import_rules
from app.modules.expenses.features.imports.schemas import (
    ColumnMapping,
    CommitIn,
    CommitOut,
    DateChoice,
    ImportEntry,
    ImportIn,
    ImportRow,
    PreviewOut,
    ProblemCode,
    Signs,
)
from app.modules.expenses.units import known_unit
from app.platform import ActivityLog, ActorContext, normalise, to_minor

EXAMPLE_DAY = dt.date(2026, 10, 31)
# Longest text an entry keeps in each field (entries/schemas.py); longer is cut, a long title
# kept whole in the note.
TITLE, MERCHANT, PERSON, NOTE, TAG = 120, 80, 60, 1000, 30
ELLIPSIS = "…"
F = ImportField


@dataclass(frozen=True)
class Reading:
    """A file read with a mapping: its rows of data and what they say."""

    fingerprint: str
    header: list[str]
    data: list[Row]
    mapping: ColumnMapping


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[: most - 1] + ELLIPSIS


def _duplicate_key(day: dt.date | str, kind: str, amount: int, title: str) -> tuple[str, ...]:
    return (str(day), str(kind), str(amount), normalise(title))


class _Problem(Exception):  # noqa: N818 - a row's reason, not an error of the request
    def __init__(self, code: ProblemCode) -> None:
        super().__init__(code)
        self.code = code


class Imports:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.rules: ImportRules = import_rules()
        self.currency = ctx.user.base_currency

    # ------------------------------------------------------------------ the file

    def read(self, body: ImportIn) -> Reading:
        limits = self.rules.limits
        if len(body.file) > limits.max_bytes:
            raise ImportTooLargeError(details={"max_bytes": limits.max_bytes})
        rows = table(decode(body.file))
        start = body.mapping.header_row if body.mapping else header_row(rows, self.rules)
        header = rows[start][1] if start < len(rows) else []
        data = rows[start + 1 :]
        if len(data) > limits.max_rows:
            raise ImportTooLargeError(details={"max_rows": limits.max_rows})
        mapping = body.mapping or self._guess(start, header, data)
        return Reading(hashlib.sha256(body.file).hexdigest(), header, data, mapping)

    def _guess(self, start: int, header: Sequence[str], data: Sequence[Row]) -> ColumnMapping:
        columns = guess_columns(header, self.rules)

        def cells(field: ImportField) -> list[str]:
            index = columns.get(field)
            return [] if index is None else [row[index] for _, row in data if index < len(row)]

        signed = False
        for text in cells(F.AMOUNT):
            try:
                parsed = parse_amount(text, self.rules)
            except ValueError:
                continue
            signed = signed or (parsed is not None and parsed[0] < 0)
        return ColumnMapping(
            header_row=start,
            columns=columns,
            date_format=guess_date_format(cells(F.DATE), self.rules),
            signs=Signs.SIGNED if signed else Signs.EXPENSE,
        )

    # ------------------------------------------------------------------ the rows

    async def rows(self, reading: Reading, *, importing: bool = False) -> list[ImportRow]:
        """Every row of data as it would be saved, or why it can't be. ``importing``: entries
        this file's import added already don't make its rows duplicates (its keys stop those
        from being added twice), so every batch sees the rows as the first one did."""
        names = await self._category_names()
        rows = [self._row(number, cells, reading.mapping, names) for number, cells in reading.data]
        await self._mark_duplicates(rows, _key_prefix(reading) if importing else None)
        return rows

    async def _category_names(self) -> dict[tuple[EntryKind, str], str]:
        """(kind, a name as compared) -> the category's id: the user's own by name, a system
        one by its key, either by its id."""
        found: dict[tuple[EntryKind, str], str] = {}
        for item in await Categories(self.ctx.scope, self.ctx.clock).all():
            if item.archived:
                continue
            for name in (item.id, item.key, item.name):
                if name:
                    found.setdefault((item.kind, normalise(name)), item.id)
        return found

    def _row(
        self,
        number: int,
        cells: Sequence[str],
        mapping: ColumnMapping,
        names: dict[tuple[EntryKind, str], str],
    ) -> ImportRow:
        def cell(field: ImportField) -> str:
            index = mapping.columns.get(field)
            return cells[index] if index is not None and index < len(cells) else ""

        problems: list[ProblemCode] = []

        def checked(work: Any) -> Any:
            try:
                return work()
            except _Problem as problem:
                problems.append(problem.code)
                return None

        day = checked(lambda: self._day(cell(F.DATE), mapping.date_format))
        title = cell(F.TITLE)
        if not title:
            problems.append(ProblemCode.MISSING_TITLE)
        money = checked(lambda: self._money(cell, mapping.signs))
        currency = cell(F.CURRENCY).upper()
        if currency and currency != self.currency:
            problems.append(ProblemCode.CURRENCY)
        if problems or day is None or money is None:
            return ImportRow(line=number, entry=None, problems=problems, duplicate=False)
        amount_minor, kind = money
        try:
            entry = self._entry(cell, day, kind, title, amount_minor, names)
        except ValidationError:
            return ImportRow(
                line=number, entry=None, problems=[ProblemCode.INVALID], duplicate=False
            )
        return ImportRow(line=number, entry=entry, problems=[], duplicate=False)

    @staticmethod
    def _day(text: str, day_format: str) -> dt.date:
        try:
            day = parse_day(text, day_format)
        except ValueError as exc:
            raise _Problem(ProblemCode.BAD_DATE) from exc
        if day is None:
            raise _Problem(ProblemCode.MISSING_DATE)
        return day

    def _money(self, cell: Any, signs: Signs) -> tuple[int, EntryKind]:
        """The amount in minor units and its kind: from a kind column or a "Dr"/"Cr" after the
        amount, else the debit or credit column it is in, else its sign."""
        said = kind_of(cell(F.KIND), self.rules) if cell(F.KIND) else None
        try:
            if cell(F.AMOUNT) or not (cell(F.DEBIT) or cell(F.CREDIT)):
                parsed = parse_amount(cell(F.AMOUNT), self.rules)
                if parsed is None:
                    raise _Problem(ProblemCode.MISSING_AMOUNT)
                value, suffix = parsed
                kind = said or suffix
                if kind is None:
                    spent = signs is Signs.EXPENSE or value < 0
                    kind = EntryKind.EXPENSE if spent else EntryKind.INCOME
            else:
                debit = parse_amount(cell(F.DEBIT), self.rules)
                credit = parse_amount(cell(F.CREDIT), self.rules)
                if debit and debit[0]:
                    value, kind = debit[0], said or EntryKind.EXPENSE
                elif credit and credit[0]:
                    value, kind = credit[0], said or EntryKind.INCOME
                else:
                    raise _Problem(ProblemCode.MISSING_AMOUNT)
        except ValueError as exc:
            raise _Problem(ProblemCode.BAD_AMOUNT) from exc
        amount_minor = to_minor(abs(value), self.currency)
        if amount_minor == 0:
            raise _Problem(ProblemCode.MISSING_AMOUNT)
        return amount_minor, kind

    def _entry(
        self,
        cell: Any,
        day: dt.date,
        kind: EntryKind,
        title: str,
        amount_minor: int,
        names: dict[tuple[EntryKind, str], str],
    ) -> ImportEntry:
        named = cell(F.CATEGORY)
        category_id = names.get((kind, normalise(named))) if named else None
        quantity, unit = self._quantity(cell(F.QUANTITY), cell(F.UNIT))
        note = cell(F.NOTE) or (title if len(title) > TITLE else "")
        mode = cell(F.PAYMENT_MODE).casefold()
        separators = "|".join(re.escape(item) for item in self.rules.tag_separators) or ";"
        tags = [_cut(tag, TAG) for tag in re.split(separators, cell(F.TAGS)) if tag.strip()]
        entry = ImportEntry(
            date=day,
            time=parse_time(cell(F.TIME), self.rules) if cell(F.TIME) else None,
            kind=kind,
            title=_cut(title, TITLE),
            amount_minor=amount_minor,
            currency=self.currency,
            category_id=category_id or self.rules.fallback_category[kind],
            category_guessed=category_id is None,
            quantity=quantity,
            unit=unit,
            merchant=_cut(cell(F.MERCHANT), MERCHANT) or None,
            payment_mode=PaymentMode(mode) if mode in set(PaymentMode) else None,
            for_whom=_cut(cell(F.FOR_WHOM), PERSON) or None,
            tags=list(dict.fromkeys(tag.strip().casefold() for tag in tags))[:MAX_TAGS],
            note=_cut(note, NOTE) or None,
        )
        entry_in(entry)  # what the import will save must be a valid entry
        return entry

    @staticmethod
    def _quantity(amount: str, unit: str) -> tuple[float | None, str | None]:
        """Both or neither: a quantity without a known unit is left out (it is only a detail)."""
        try:
            value = float(amount.replace(",", ""))
            return (value, known_unit(unit.casefold())) if value > 0 else (None, None)
        except ValueError:
            return None, None

    async def _mark_duplicates(self, rows: list[ImportRow], own: str | None) -> None:
        entries = [row.entry for row in rows if row.entry]
        if not entries:
            return
        days = [entry.date.isoformat() for entry in entries]
        criteria = EntryRepository(self.ctx.scope, self.ctx.clock).query(
            {"date": {"$gte": min(days), "$lte": max(days)}}
            | ({"client_key": {"$not": {"$regex": f"^{re.escape(own)}"}}} if own else {})
        )
        fields = {"date": 1, "kind": 1, "amount_minor": 1, "title": 1}
        saved = {
            _duplicate_key(item["date"], item["kind"], item["amount_minor"], item["title"])
            async for item in Entry.get_pymongo_collection().find(criteria, fields)
        }
        for row in rows:
            entry = row.entry
            if entry:
                key = _duplicate_key(entry.date, entry.kind, entry.amount_minor, entry.title)
                row.duplicate = key in saved

    # ------------------------------------------------------------------ the preview

    async def preview(self, body: ImportIn) -> PreviewOut:
        reading = self.read(body)
        rows = await self.rows(reading)
        limits = self.rules.limits
        stopped = [row for row in rows if row.problems]
        named = reading.mapping.columns.get(F.CATEGORY)
        unknown = sorted(
            {
                cells[named]
                for (_, cells), row in zip(reading.data, rows, strict=True)
                if named is not None
                and row.entry
                and row.entry.category_guessed
                and named < len(cells)
                and cells[named]
            }
        )
        return PreviewOut(
            fingerprint=reading.fingerprint,
            columns=reading.header,
            mapping=reading.mapping,
            date_formats=[
                DateChoice(format=item, example=EXAMPLE_DAY.strftime(item))
                for item in self.rules.date_formats
            ],
            rows=rows[: limits.preview_rows],
            problems=stopped[: limits.max_problems],
            unknown_categories=unknown,
            total=len(rows),
            ready=sum(1 for row in rows if row.entry and not row.duplicate),
            duplicates=sum(1 for row in rows if row.duplicate),
            with_problems=len(stopped),
        )

    # ------------------------------------------------------------------ the import

    async def commit(self, body: CommitIn) -> CommitOut:
        """Add the next batch of the file's importable rows (from ``body.offset``)."""
        reading = self.read(body)
        prefix = _key_prefix(reading)
        rows = await self.rows(reading, importing=True)
        importable = [
            row for row in rows if row.entry and (body.include_duplicates or not row.duplicate)
        ]
        batch = importable[body.offset : body.offset + self.rules.limits.commit_batch]
        entries = EntryService(self.ctx)
        added = 0
        for row in batch:
            assert row.entry is not None  # noqa: S101 - importable rows have an entry
            _, created = await entries.create(
                entry_in(row.entry), f"{prefix}{row.line}", EntrySource.IMPORT, noted=False
            )
            added += created
        done = body.offset + len(batch)
        finished = done >= len(importable)
        if finished and added:
            await self._note(prefix)
        return CommitOut(
            added=added,
            already=len(batch) - added,
            done=min(done, len(importable)),
            total=len(importable),
            next_offset=None if finished else done,
        )

    async def _note(self, prefix: str) -> None:
        """Once, as the last batch adds its rows: how many entries the file brought in."""
        criteria = EntryRepository(self.ctx.scope, self.ctx.clock).query(
            {"client_key": {"$regex": f"^{re.escape(prefix)}"}}
        )
        count = await Entry.find(criteria).count()
        log = ActivityLog(self.ctx.scope, self.ctx.clock, self.ctx.settings)
        await log.record(f"{MODULE}.entries_imported", module=MODULE, count=count)


def _key_prefix(reading: Reading) -> str:
    """Rows of one file are keyed by its fingerprint: the same file adds each row once."""
    return f"import:{reading.fingerprint}:"


def entry_in(entry: ImportEntry) -> EntryIn:
    """The entry the import saves for a row."""
    return EntryIn.model_validate(
        entry.model_dump(exclude={"category_guessed", "currency"}, mode="json")
    )
