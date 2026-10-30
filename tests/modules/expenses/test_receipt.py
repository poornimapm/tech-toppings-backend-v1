"""The receipt reader (scan-receipt's static mode): a receipt's text, as a phone reads it, to
draft expenses that add up to the amount paid."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.modules.expenses import parsing
from app.modules.expenses.features.categories.service import system_categories
from app.modules.expenses.tools import MAX_ENTRY_DRAFTS, expenses_draft
from app.platform.agents.tools import TOOL_ATTRIBUTE, ToolSpec

SKILLS = Path(__file__).resolve().parents[3] / "app" / "modules" / "expenses" / "skills"
LEXICON = parsing.load(SKILLS / "track-expenses" / "references")
WORDS = parsing.load_receipt(SKILLS / "scan-receipt" / "references")
TODAY = date(2026, 10, 29)
TAMIL_FORTY = "\N{TAMIL DIGIT FOUR}\N{TAMIL DIGIT ZERO}.\N{TAMIL DIGIT ZERO}\N{TAMIL DIGIT ZERO}"
CATEGORIES: dict[str, set[str]] = {}
for _category in system_categories():
    CATEGORIES.setdefault(_category.kind, set()).add(_category.id)


DRAFT_TOOL: ToolSpec = getattr(expenses_draft, TOOL_ATTRIBUTE)


def read(text: str, locale: str = "en", **habits: str) -> parsing.Receipt:
    return parsing.read_receipt(
        text,
        TODAY,
        LEXICON,
        WORDS,
        locale=locale,
        max_drafts=MAX_ENTRY_DRAFTS,
        habits=habits,
        categories=CATEGORIES,
    )


def handed_over(receipt: parsing.Receipt) -> int:
    """How many drafts the drafts tool takes from it (it refuses what it can't)."""
    arguments = [parsing.arguments(d, LEXICON.uncertain_below) for d in receipt.drafts]
    return len(DRAFT_TOOL.parse({"drafts": arguments}).model_dump()["drafts"])


def rows(receipt: parsing.Receipt) -> list[tuple[str | None, Decimal | None]]:
    return [(draft.title, draft.amount) for draft in receipt.drafts]


def unsure(receipt: parsing.Receipt) -> list[list[str]]:
    return [parsing.arguments(d, LEXICON.uncertain_below)["uncertain"] for d in receipt.drafts]


GROCERY = """TAX INVOICE
SRI MURUGAN STORES
12, Gandhi Road, Chennai - 600017
Ph: 044-2345678  GSTIN 33ABCDE1234F1Z5
Bill No: 1234   Date: 12/10/2026  Time 10:30
Item            Qty   Rate    Amount
1 Tomato 1kg     2    40.00    80.00
2 Toor Dal 1 Kg  1   145.00   145.00
3 Milk 500ml     2 x 27.00     54.00
Sub Total                      279.00
CGST 2.5%                        6.64
SGST 2.5%                        6.64
Total Qty: 5
Grand Total                   279.00
Cash                          500.00
Change                        221.00
Thank you visit again"""


def test_reads_the_items_shop_and_day_and_skips_everything_else() -> None:
    receipt = read(GROCERY)

    assert rows(receipt) == [
        ("Tomato 1kg", Decimal("80.00")),
        ("Toor Dal 1 Kg", Decimal("145.00")),
        ("Milk 500ml", Decimal("54.00")),
    ]
    assert receipt.total == Decimal("279.00")
    first = receipt.drafts[0]
    assert (first.merchant, first.date, first.category_id) == (
        "Sri Murugan Stores",
        date(2026, 10, 12),
        "groceries",
    )
    # A size in the title times the count column: 2 x 1 kg, 2 x 500 ml.
    assert [(d.quantity, d.unit) for d in receipt.drafts] == [
        (Decimal(2), "kg"),
        (Decimal(1), "kg"),
        (Decimal(1000), "ml"),
    ]
    assert unsure(receipt) == [[], [], []]


def test_tax_already_in_the_prices_is_not_added_again() -> None:
    # GROCERY lists CGST and SGST, but its total is the items' sum: no draft for them.
    assert sum(d.amount or 0 for d in read(GROCERY).drafts) == Decimal("279.00")


def test_charges_on_top_of_the_items_become_one_draft_filed_with_them() -> None:
    receipt = read(
        """HOTEL SARAVANA BHAVAN
Date 11-10-2026
Masala Dosa  2  90.00  180.00
Filter Coffee 2 30.00 60.00
Sub Total 240.00
Service Charge 12.00
CGST 2.5% 6.30
SGST 2.5% 6.30
Grand Total Rs.264.60
UPI 264.60"""
    )

    assert rows(receipt)[-1] == ("Service Charge + CGST 2.5% + SGST 2.5%", Decimal("24.60"))
    assert {d.category_id for d in receipt.drafts} == {"food"}
    assert sum(d.amount or 0 for d in receipt.drafts) == Decimal("264.60")
    assert unsure(receipt)[-1] == []


def test_a_total_above_what_explains_it_is_the_rest_of_the_receipt_to_check() -> None:
    receipt = read("Big Bazaar\nRice 5kg 320.00\nGrand Total 450.00")

    assert rows(receipt) == [
        ("Rice 5kg", Decimal("320.00")),
        ("Rest of the receipt", Decimal("130.00")),
    ]
    assert "amount" in unsure(receipt)[-1]
    assert rows(read("Big Bazaar\nRice 5kg 320.00\nGrand Total 450.00", "ta"))[-1][0] == (
        "ரசீதின் மீதி"
    )


def test_a_discount_comes_off_the_largest_item_for_the_user_to_check() -> None:
    receipt = read(
        "APOLLO PHARMACY\nParacetamol 500 Tab 15s 32.50\nVitamin C 60.00\n"
        "Discount 9.25\nNet Amount 83.25"
    )

    assert rows(receipt) == [
        ("Paracetamol 500 Tab 15s", Decimal("32.50")),
        ("Vitamin C", Decimal("50.75")),
    ]
    assert unsure(receipt) == [["date"], ["amount", "date"]]
    assert {d.category_id for d in receipt.drafts} == {"health"}  # the kind of shop


def test_a_total_far_below_the_items_marks_every_amount() -> None:
    receipt = read("Shop\nSoap 40.00\nOil 160.00\nTotal 20.00")

    assert all("amount" in fields for fields in unsure(receipt))
    assert rows(receipt) == [("Soap", Decimal("40.00")), ("Oil", Decimal("160.00"))]


def test_only_a_total_is_one_draft_named_for_the_shop() -> None:
    assert rows(read("INDIAN OIL PETROL PUMP\nTotal Rs 1,500.00")) == [
        ("Indian Oil Petrol Pump", Decimal("1500.00"))
    ]
    assert read("INDIAN OIL PETROL PUMP\nTotal Rs 1,500.00").drafts[0].category_id == "fuel"
    assert rows(read("Total 99.00", "hi")) == [("रसीद", Decimal("99.00"))]


def test_nothing_readable_is_no_drafts() -> None:
    assert read("~~~ ### ~~~\n\n***").drafts == []


@pytest.mark.parametrize(
    ("line", "amount"),
    [
        ("Ghee 1L 1,250.00", "1250.00"),
        ("TV 1,23,456.00", "123456.00"),
        ("Bread 45,50", "45.50"),
        ("Butter 8O.0O", "80.00"),  # look-alike letters in a price
        ("Curd ₹35", "35.00"),
        ("Eggs 72/-", "72.00"),
        ("Sugar Rs.48.5", "48.50"),
        (f"தக்காளி {TAMIL_FORTY}", "40.00"),  # Tamil digits
    ],
)
def test_figures_as_printed(line: str, amount: str) -> None:
    assert read(line).drafts[0].amount == Decimal(amount)


def test_lines_that_are_not_things_bought_are_left_out() -> None:
    receipt = read(
        "Kirana\nSugar 48.00\nMobile 9876543210\nPaid by Card 48.00\nBill No 5521\n"
        "Change 2.00\nYou saved 5.00\nPoints 12"
    )

    assert rows(receipt) == [("Sugar", Decimal("48.00"))]


def test_dates_are_read_day_first_and_never_in_the_future_or_long_ago() -> None:
    assert read("Shop\n05/10/26\nTea 10.00").drafts[0].date == date(2026, 10, 5)
    assert read("Shop\n2026-10-07\nTea 10.00").drafts[0].date == date(2026, 10, 7)
    assert read("Shop\n12 Oct 2026\nTea 10.00").drafts[0].date == date(2026, 10, 12)
    assert read("Shop\n30/11/2026\nTea 10.00").drafts[0].date is None  # tomorrow+
    assert read("Shop\n01/01/2020\nTea 10.00").drafts[0].date is None  # too long ago


def test_the_users_own_habit_files_an_item_first() -> None:
    receipt = read("Mart\nDiapers 499.00", diapers="personal_care")

    assert receipt.drafts[0].category_id == "personal_care"


@pytest.mark.parametrize(
    ("lines", "extra", "total"),
    [
        (45, "", "450.00"),  # more lines than the tool takes
        (40, "Service Charge 20.00\n", "420.00"),  # as many, and a charge on top
        (45, "Service Charge 20.00\n", "470.00"),
    ],
)
def test_a_long_receipt_fits_the_drafts_tool(lines: int, extra: str, total: str) -> None:
    items = "\n".join(f"Item {chr(65 + i % 26)}{i} 10.00" for i in range(lines))
    receipt = read(f"Mart\n{items}\n{extra}Grand Total {total}")

    assert handed_over(receipt) == len(receipt.drafts) <= MAX_ENTRY_DRAFTS
    assert "Rest of the receipt" in [d.title for d in receipt.drafts]
    assert sum(d.amount or 0 for d in receipt.drafts) == Decimal(total)


def test_tamil_and_hindi_receipts() -> None:
    tamil = read("ஸ்ரீ முருகன் ஸ்டோர்ஸ்\nதக்காளி 1 கிலோ 40.00\nஅரிசி 5 கிலோ 320.00\nமொத்தம் 360.00", "ta")
    hindi = read("शर्मा किराना स्टोर\nदूध 1 लीटर 60.00\nचीनी 1 किलो 45.00\nकुल 105.00", "hi")

    assert rows(tamil) == [("தக்காளி 1 கிலோ", Decimal("40.00")), ("அரிசி 5 கிலோ", Decimal("320.00"))]
    assert [(d.quantity, d.unit, d.category_id) for d in tamil.drafts] == [
        (Decimal(1), "kg", "groceries"),
        (Decimal(5), "kg", "groceries"),
    ]
    assert rows(hindi) == [("दूध 1 लीटर", Decimal("60.00")), ("चीनी 1 किलो", Decimal("45.00"))]
    assert tamil.drafts[0].merchant == "ஸ்ரீ முருகன் ஸ்டோர்ஸ்"


# ------------------------------------------------------------------ what isn't an item


@pytest.mark.parametrize(
    "line",
    [
        "Subtotal 150.00",  # one word, not "sub total"
        "Chandni Chowk, Delhi 110006",  # a postal code
        "Bill: 3381",
        "KOT 44",
        "12 Oct 2026",  # a date on its own
        "12 Oct 26",
    ],
)
def test_numbers_that_are_not_prices_are_not_items(line: str) -> None:
    receipt = read(f"GUPTA SWEETS\n{line}\nKaju Katli 450.00\nSamosa 80.00\nTotal 530.00")

    assert rows(receipt) == [("Kaju Katli", Decimal("450.00")), ("Samosa", Decimal("80.00"))]
    assert receipt.total == Decimal("530.00")


def test_a_payment_line_never_stands_for_the_total() -> None:
    paid = read(
        "Shop\nRice 100.00\nSoap 50.00\nNet Amount 150.00\n"
        "Cash Received Amount 200.00\nBalance Amount 50.00"
    )
    tendered = read("Shop\nRice 100.00\nTotal 100.00\nTotal Tendered 200.00")

    assert paid.total == Decimal("150.00")
    assert rows(paid) == [("Rice", Decimal("100.00")), ("Soap", Decimal("50.00"))]
    assert (tendered.total, rows(tendered)) == (Decimal("100.00"), [("Rice", Decimal("100.00"))])


@pytest.mark.parametrize("line", ["Net Amt 150.00", "Bill Amt 150.00", "NET AMT. 150.00"])
def test_short_forms_of_the_total(line: str) -> None:
    receipt = read(f"Shop\nRice 100.00\nSoap 50.00\n{line}")

    assert receipt.total == Decimal("150.00")
    assert len(receipt.drafts) == 2


def test_a_column_label_after_an_item_is_cut_off() -> None:
    receipt = read("Shop\nSugar 1kg MRP 50.00 45.00\nREF OIL 1L 150.00\nTotal 195.00")

    assert rows(receipt) == [("Sugar 1kg", Decimal("45.00")), ("REF OIL 1L", Decimal("150.00"))]


def test_a_garbled_tax_line_is_still_a_tax_line() -> None:
    # The camera read "CGST 2.5%" as "FB GESI 2.5%": a rate says what it is.
    receipt = read("Shop\nRice 100.00\nFB GESI 2.5% 2.50\nSGSIE2.5% 2.50\nTotal 100.00")

    assert rows(receipt) == [("Rice", Decimal("100.00"))]


# ------------------------------------------------------------------ Tamil and Hindi words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # कर (tax) is the start of करी (curry) and शक्कर ends in it: neither is a tax.
        ("ढाबा\nचिकन करी 220.00\nशक्कर 45.00\nकुल योग 265.00", ["चिकन करी", "शक्कर"]),
        # कुल (total) is not कुल्फी or कुल्लड़.
        ("ढाबा\nकुल्फी 60.00\nकुल्लड़ चाय 40.00\nकुल 100.00", ["कुल्फी", "कुल्लड़ चाय"]),
        # Two-letter words with a vowel sign are words: घी (ghee), டீ (tea).
        ("किराना\nघी 500.00\nचाय 40.00\nकुल 540.00", ["घी", "चाय"]),
        ("கடை\nடீ 15.00\nகாபி 20.00\nமொத்தம் 35.00", ["டீ", "காபி"]),
    ],
)
def test_tamil_and_hindi_words_match_whole_or_at_a_word_start(
    text: str, expected: list[str]
) -> None:
    assert [d.title for d in read(text, "hi").drafts] == expected


# ------------------------------------------------------------------ dates, counts, shops


@pytest.mark.parametrize("line", ["Order 12-Jan-2026 16:05", "Date: 05 January 2026"])
def test_january_by_name(line: str) -> None:
    assert read(f"Cafe\n{line}\nTea 10.00", "en").drafts[0].date is not None


@pytest.mark.parametrize(
    ("line", "quantity"),
    [
        ("Bread 25.00 50.00", (Decimal(2), "piece")),  # a price column, not 25 pieces
        ("Tomato 1.500 40.00 60.00", (Decimal("1.500"), "kg")),  # a weight column
        ("Onion 1.250 KG 30.00 37.50", (Decimal("1.250"), "kg")),
        ("Coke 2 x 750 ml 40.00 80.00", (Decimal(1500), "ml")),
        ("Milk 2 50.00", (Decimal(2), "piece")),  # a count column
    ],
)
def test_quantities_come_from_counts_and_weights_not_prices(
    line: str, quantity: tuple[Decimal, str]
) -> None:
    [draft] = read(f"Shop\n{line}").drafts

    assert (draft.quantity, draft.unit) == quantity


def test_shop_names_are_tidied() -> None:
    assert read("HALDIRAM'S\nTea 10.00").drafts[0].merchant == "Haldiram's"
    assert read("TRENDS FASHION        ]\nShirt 10.00").drafts[0].merchant == "Trends Fashion"
    assert read("D-MART (AVENUE SUPERMARTS)\nTea 10.00").drafts[0].merchant == (
        "D-Mart (Avenue Supermarts)"
    )


def test_a_receipt_with_only_a_total_is_filed_by_its_kind_of_shop() -> None:
    [draft] = read("INDIAN OIL CORPORATION LTD\nAmount (Rs): 500.00").drafts

    assert draft.category_id == "fuel"  # not groceries, for the word "oil"
