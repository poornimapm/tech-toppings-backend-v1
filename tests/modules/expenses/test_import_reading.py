"""Reading a CSV file for import: text, rows, columns, days and amounts as people write them."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.imports.reading import (
    decode,
    guess_columns,
    guess_date_format,
    header_row,
    parse_amount,
    table,
)
from app.modules.expenses.features.imports.rules import ImportField, import_rules

RULES = import_rules()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("₹1,23,456.50", (Decimal("123456.50"), None)),
        ("1250", (Decimal("1250"), None)),
        ("-20", (Decimal("-20"), None)),
        ("(500.00)", (Decimal("-500.00"), None)),
        ("500 Dr", (Decimal("500"), EntryKind.EXPENSE)),
        ("1,250.50 CR", (Decimal("1250.50"), EntryKind.INCOME)),
        ("Rs. 99", (Decimal("99"), None)),
        ("", None),
    ],
)
def test_reads_amounts_as_people_write_them(
    text: str, expected: tuple[Decimal, EntryKind | None] | None
) -> None:
    assert parse_amount(text, RULES) == expected


@pytest.mark.parametrize("text", ["abc", "1.2.3", "--", "12/10"])
def test_refuses_what_is_not_an_amount(text: str) -> None:
    with pytest.raises(ValueError, match="not an amount"):
        parse_amount(text, RULES)


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (["2026-10-01", "2026-10-31"], "%Y-%m-%d"),
        (["01/10/2026", "13/10/2026"], "%d/%m/%Y"),  # day first, as in India
        (["10/13/2026", "10/01/2026"], "%m/%d/%Y"),  # only month first reads 13
        (["05-Oct-2026"], "%d-%b-%Y"),
        (["05/10/26", ""], "%d/%m/%y"),
    ],
)
def test_guesses_the_date_format_that_reads_every_day(days: list[str], expected: str) -> None:
    assert guess_date_format(days, RULES) == expected


def test_finds_the_header_below_a_bank_statement_s_own_lines() -> None:
    rows = table(
        "Account statement\n"
        "Statement period,01/10/2026 to 31/10/2026\n"
        "\n"
        "Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/10/2026,Tea,1,20.00,,980.00\n"
    )
    start = header_row(rows, RULES)
    assert rows[start] == (
        4,  # the empty line is left out, but rows keep their numbers
        ["Date", "Narration", "Chq./Ref.No.", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
    )
    assert guess_columns(rows[start][1], RULES) == {
        ImportField.DATE: 0,
        ImportField.TITLE: 1,
        ImportField.DEBIT: 3,
        ImportField.CREDIT: 4,
    }


def test_names_columns_in_tamil_and_hindi() -> None:
    assert guess_columns(["தேதி", "விவரம்", "தொகை"], RULES) == {
        ImportField.DATE: 0,
        ImportField.TITLE: 1,
        ImportField.AMOUNT: 2,
    }
    assert guess_columns(["तारीख", "विवरण", "राशि"], RULES) == {
        ImportField.DATE: 0,
        ImportField.TITLE: 1,
        ImportField.AMOUNT: 2,
    }


def test_reads_semicolons_and_tabs_and_the_files_spreadsheets_save() -> None:
    assert table("date;title\n2026-10-01;Tea\n")[1] == (2, ["2026-10-01", "Tea"])
    assert table("date\ttitle\n2026-10-01\tTea\n")[1] == (2, ["2026-10-01", "Tea"])
    assert decode("﻿date".encode()) == "date"  # the byte-order mark is not a header
    assert decode("Café".encode("cp1252")) == "Café"
