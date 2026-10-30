"""The slip reader the modules share (app/platform/slips): a printed slip's lines as OCR reads
them, sorted into items, totals and what was added, with the shop and the day. What each module
makes of a slip is tested with the module (expenses' receipts, receipts' bills)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.platform import read_slip, word_pattern

TODAY = date(2026, 10, 30)
SLIP = """|| SRI MURUGAN STORES (
12, Bazaar Street, Madurai 625001
Ph: 98430 12345
Date: 12-Oct-2026
Tomato 1kg      2 x 40.00   80.00
Toor Dal 1 Kg   1 145.OO  145.OO
CGST 2.5%                    5.63
Sub Total                  225.00
Grand Total                230.63
Cash                       500.00"""


def test_a_slip_is_sorted_into_items_totals_and_what_was_added() -> None:
    slip = read_slip(SLIP, TODAY, max_age_days=400, max_amount=Decimal(200000))

    assert slip.merchant == "Sri Murugan Stores"  # the paper's edge and the shout tidied away
    assert slip.day == date(2026, 10, 12)  # a month by its name
    assert [(name, line.amount) for line, name in slip.items] == [
        ("Tomato 1kg", Decimal("80.00")),
        ("Toor Dal 1 Kg", Decimal("145.00")),  # look-alike letters read as digits
    ]
    assert slip.added == [("CGST 2.5%", Decimal("5.63"))]
    assert slip.total == Decimal("230.63")  # the last total, never the subtotal or the cash


def test_the_caller_says_how_old_a_date_and_how_large_a_figure_may_be() -> None:
    old = "Croma\nDate: 14/03/2022\nKettle  1,495.00\nTotal  1,495.00"
    dear = "Maruti Arena\nSwift VXI  7,45,000.00\nTotal  7,45,000.00"

    recent = read_slip(old, TODAY, max_age_days=400, max_amount=Decimal(200000))
    kept = read_slip(old, TODAY, max_age_days=3650, max_amount=Decimal(200000))
    small = read_slip(dear, TODAY, max_age_days=400, max_amount=Decimal(200000))
    large = read_slip(dear, TODAY, max_age_days=400, max_amount=Decimal(5000000))

    assert (recent.day, kept.day) == (None, date(2022, 3, 14))
    assert (small.items, small.total) == ([], None)  # a phone number's size: not a price
    assert large.total == Decimal("745000.00")


def test_a_date_after_today_is_a_misread() -> None:
    slip = read_slip(
        "Shop\nDate 05/11/2026\nTea 20.00", TODAY, max_age_days=400, max_amount=Decimal(1000)
    )

    assert slip.day is None


def test_words_match_whole_in_english_and_as_stems_in_tamil_and_hindi() -> None:
    pattern = word_pattern(["total", "மொத்தம்", "भुगतान", "कुल"])

    assert pattern.search("grand total")
    assert not pattern.search("totally")
    assert pattern.search("மொத்தம்:")
    assert pattern.search("भुगतानों")  # with an ending
    assert pattern.search("कुल राशि")
    assert not pattern.search("कुल्फी")  # short words match whole only
