"""The bill reader (scan-bill's static mode): a bill's text, as a phone reads it, to one receipt
to keep, and the skill handing it to the form without AI."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.modules.receipts.bill import Bill, load_bill, read_bill
from app.modules.receipts.tools import receipts_draft
from app.platform.agents.tools import TOOL_ATTRIBUTE, ToolSpec
from tests.helpers import register
from tests.platform.agents.conftest import AgentClient, ask

REFERENCES = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "modules"
    / "receipts"
    / "skills"
    / "scan-bill"
    / "references"
)
WORDS = load_bill(REFERENCES)
TODAY = date(2026, 10, 30)
DRAFT_TOOL: ToolSpec = getattr(receipts_draft, TOOL_ATTRIBUTE)

BILL = """VIJAY SALES
Electronics & Home Appliances, Anna Nagar, Chennai 600040
TAX INVOICE
Invoice No: VS/26/04217      Date: 14/03/2025
1  Samsung 253L Refrigerator RT28   1  28,490.00  28,490.00
2  Stabilizer V-Guard               1   1,650.00   1,650.00
S/N: RT28C3032S8-0042
Warranty: 1 Year, 10 Years on Compressor
CGST 9%   2,299.32
SGST 9%   2,299.32
Grand Total                         30,140.00
Card                                30,140.00"""


def read(text: str) -> Bill:
    bill = read_bill(text, TODAY, WORDS)
    assert bill is not None
    return bill


def test_a_bill_becomes_one_receipt_with_its_numbers_and_warranty() -> None:
    bill = read(BILL)

    assert bill.text == "Samsung 253L Refrigerator RT28"  # the dearest thing on it
    assert (bill.shop, bill.bought_on) == ("Vijay Sales", date(2025, 3, 14))  # a bill years old
    assert bill.amount == Decimal("30140.00")  # the total paid, not the item's price
    assert (bill.bill_number, bill.serial_number) == ("VS/26/04217", "RT28C3032S8-0042")
    assert bill.warranty_months == 12  # the thing's own, not the compressor's
    assert sorted(bill.uncertain) == ["text", "warranty_months"]


def test_what_it_hands_over_is_what_the_tool_takes() -> None:
    arguments = read(BILL).arguments()

    parsed = DRAFT_TOOL.parse({"draft": arguments})

    assert parsed.model_dump()["draft"]["warranty_months"] == 12
    assert arguments["bought_on"] == "2025-03-14"
    assert arguments["amount"] == 30140.0


@pytest.mark.parametrize(
    ("line", "months"),
    [
        ("Warranty: 2 Years", 24),
        ("1 Yr Warranty", 12),
        ("WARRANTY 6 MONTHS", 6),
        ("Guarantee 3yrs", 36),
        ("வாரண்டி 1 வருடம்", 12),
        ("वारंटी 2 साल", 24),
        ("गारंटी 18 महीने", 18),
    ],
)
def test_a_warranty_is_read_in_months(line: str, months: int) -> None:
    bill = read(f"Croma\nPhilips Iron   1  1,295.00\n{line}\nTotal 1,295.00")

    assert bill.warranty_months == months
    assert "warranty_months" not in bill.uncertain


def test_a_warranty_may_be_on_the_next_line() -> None:
    bill = read("Croma\nPhilips Iron   1,295.00\nWarranty\n1 Year\nTotal 1,295.00")

    assert bill.warranty_months == 12


def test_no_warranty_is_ever_guessed() -> None:
    bill = read(
        "Croma\nPhilips Iron   1,295.00\nGoods once sold won't be taken back\nTotal 1,295.00"
    )

    assert bill.warranty_months is None


def test_a_warranty_no_warranty_has_is_a_misread() -> None:
    bill = read("Croma\nPhilips Iron   1,295.00\nWarranty 200 years\nTotal 1,295.00")

    assert bill.warranty_months is None


def test_without_a_total_the_dearest_item_is_the_amount_to_check() -> None:
    bill = read("Lakshmi Electricals\nCeiling Fan 1200mm   2,150.00")

    assert (bill.text, bill.amount) == ("Ceiling Fan 1200mm", Decimal("2150.00"))
    assert {"amount", "bought_on"} <= set(bill.uncertain)  # and no date to be read


def test_number_lines_are_never_the_thing_bought() -> None:
    bill = read("Croma\nS/N 99881\nIMEI 356789012345678\nKettle   1,495.00\nTotal 1,495.00")

    assert bill.text == "Kettle"
    assert bill.serial_number == "99881"  # the first printed
    assert "text" not in bill.uncertain


def test_a_bill_dated_after_today_has_no_date() -> None:
    bill = read("Croma\nKettle   1,495.00\nDate 30/11/2026\nTotal 1,495.00")

    assert bill.bought_on is None
    assert "bought_on" in bill.uncertain


def test_nothing_readable_is_no_bill() -> None:
    assert read_bill("~~ ## ~~\nthank you", TODAY, WORDS) is None


async def test_the_skill_fills_the_form_without_ai(agent_client: AgentClient) -> None:
    async with agent_client() as client:
        account = await register(client)
        run = await ask(
            client, account, BILL, skill="scan-bill", locale="en", context={"input_type": "ocr"}
        )
        photo = await client.post(
            "/v1/agents/assistant/photo-runs?skill=scan-bill",
            content=b"\xff\xd8\xff\xe0" + b"BILL" * 50,
            headers=account.headers | {"Content-Type": "image/jpeg"},
        )

    assert (run["mode"], run["message_code"]) == ("static", "scan-bill.ready")
    assert run["message_params"] == {"total": 30140.0}
    [action] = run["client_actions"]
    assert action["tool"] == "receipts_draft"
    assert action["arguments"]["draft"]["text"] == "Samsung 253L Refrigerator RT28"
    # A photo without AI: the app is told to read it on the device.
    assert photo.json()["message_code"] == "scan-bill.on_device"


async def test_unreadable_text_hands_nothing_over(client: AsyncClient) -> None:
    account = await register(client)

    run = await ask(client, account, "~~ ## ~~", skill="scan-bill", context={"input_type": "ocr"})

    assert (run["message_code"], run["client_actions"]) == ("scan-bill.unreadable", [])
