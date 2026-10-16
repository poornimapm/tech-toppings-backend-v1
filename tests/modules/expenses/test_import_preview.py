"""``POST /v1/expenses/import/preview``: a CSV file read into entries without saving anything,
with a guessed mapping the user can change, problem rows and rows saved already marked."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import ApiSettings
from app.modules.expenses.features.imports.router import ENVELOPE_BYTES
from app.modules.expenses.features.imports.rules import import_rules
from tests.helpers import Account, register
from tests.modules.expenses.conftest import CATEGORIES, add, listed

PREVIEW = "/v1/expenses/import/preview"
STATEMENT = (
    "Account statement for XXXX1234\n"
    "Statement period,01/10/2026 to 31/10/2026\n"
    "\n"
    "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    '01/10/2026,UPI-SWIGGY-12345,0001,01/10/2026,"1,250.50",,"48,749.50"\n'
    '03/10/2026,SALARY OCT,0002,03/10/2026,,"50,000.00","98,749.50"\n'
    '05/10/2026,ATM WDL,0003,05/10/2026,2000,,"96,749.50"\n'
)


def encoded(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return base64.b64encode(data).decode()


async def preview(
    client: AsyncClient, account: Account, text: str | bytes, mapping: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"file": encoded(text)} | ({"mapping": mapping} if mapping else {})
    response = await client.post(PREVIEW, headers=account.headers, json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def brief(row: dict[str, Any]) -> tuple[Any, ...]:
    entry = row["entry"]
    return (row["line"], entry["date"], entry["kind"], entry["title"], entry["amount_minor"])


async def test_reads_a_bank_statement_with_debit_and_credit_columns(client: AsyncClient) -> None:
    account = await register(client)
    answer = await preview(client, account, STATEMENT)

    assert answer["columns"][:2] == ["Date", "Narration"]
    assert answer["mapping"] == {
        "header_row": 2,
        "columns": {"date": 0, "title": 1, "debit": 4, "credit": 5},
        "date_format": "%d/%m/%Y",
        "signs": "expense",
    }
    assert [brief(row) for row in answer["rows"]] == [
        (5, "2026-10-01", "expense", "UPI-SWIGGY-12345", 125050),
        (6, "2026-10-03", "income", "SALARY OCT", 5000000),
        (7, "2026-10-05", "expense", "ATM WDL", 200000),
    ]
    first = answer["rows"][0]["entry"]
    assert (first["category_id"], first["category_guessed"]) == ("other_expense", True)
    assert answer["rows"][1]["entry"]["category_id"] == "other_income"
    assert (answer["total"], answer["ready"], answer["duplicates"], answer["with_problems"]) == (
        3,
        3,
        0,
        0,
    )
    assert {"format": "%d/%m/%Y", "example": "31/10/2026"} in answer["date_formats"]
    assert len(answer["fingerprint"]) == 64
    assert (await listed(client, account))["total"] == 0  # nothing saved


async def test_reads_its_own_export_back_and_knows_it_is_saved(client: AsyncClient) -> None:
    account = await register(client)
    pets = (
        await client.post(
            CATEGORIES,
            headers=account.headers,
            json={"name": "Pets", "icon": "🐶", "accent": "teal"},
        )
    ).json()
    await add(
        client,
        account,
        title="Dog food",
        amount_minor=90000,
        category_id=pets["id"],
        date="2026-10-05",
        quantity=2,
        unit="kg",
        tags=["pets", "monthly"],
        payment_mode="upi",
    )
    await add(
        client, account, title="Salary", kind="income", category_id="salary", amount_minor=5000000
    )
    exported = await client.get("/v1/expenses/export.csv", headers=account.headers)

    answer = await preview(client, account, exported.content)
    assert set(answer["mapping"]["columns"]) == {
        "date", "time", "kind", "title", "amount", "currency", "category", "quantity", "unit",
        "merchant", "payment_mode", "for_whom", "tags", "note",
    }  # fmt: skip
    dog = next(row["entry"] for row in answer["rows"] if row["entry"]["title"] == "Dog food")
    assert dog | {"time": None} == {
        "date": "2026-10-05",
        "time": None,
        "kind": "expense",
        "title": "Dog food",
        "amount_minor": 90000,
        "currency": "INR",
        "category_id": pets["id"],  # the user's category, by its name
        "category_guessed": False,
        "quantity": 2.0,
        "unit": "kg",
        "merchant": None,
        "payment_mode": "upi",
        "for_whom": None,
        "tags": ["pets", "monthly"],
        "note": None,
    }
    assert all(row["duplicate"] for row in answer["rows"])
    assert (answer["ready"], answer["duplicates"]) == (0, 2)


async def test_a_signed_amount_column_tells_spent_from_received(client: AsyncClient) -> None:
    account = await register(client)
    answer = await preview(
        client, account, "Date,Description,Amount\n2026-10-01,Tea,-20\n2026-10-02,Refund,150.5\n"
    )
    assert answer["mapping"]["signs"] == "signed"
    assert [(row["entry"]["kind"], row["entry"]["amount_minor"]) for row in answer["rows"]] == [
        ("expense", 2000),
        ("income", 15050),
    ]
    # All money spent, if the user says so.
    mapping = answer["mapping"] | {"signs": "expense"}
    again = await preview(
        client, account, "Date,Description,Amount\n2026-10-02,Refund,150.5\n", mapping
    )
    assert again["rows"][0]["entry"]["kind"] == "expense"


async def test_lists_each_row_a_problem_stops_with_its_reasons(client: AsyncClient) -> None:
    account = await register(client)
    text = (
        "date,title,amount,currency\n"
        "2026-10-01,Tea,20,INR\n"
        "31/02/2026,Bad day,20,\n"
        "2026-10-02,,20,\n"
        "2026-10-03,Odd,abc,\n"
        "2026-10-04,Dollars,5,USD\n"
        ",Nothing,,\n"
    )
    answer = await preview(client, account, text)
    assert [(row["line"], row["problems"]) for row in answer["problems"]] == [
        (3, ["bad_date"]),
        (4, ["missing_title"]),
        (5, ["bad_amount"]),
        (6, ["currency"]),  # entries are kept in the base currency
        (7, ["missing_date", "missing_amount"]),
    ]
    assert (answer["total"], answer["ready"], answer["with_problems"]) == (6, 1, 5)


async def test_the_user_can_change_the_mapping(client: AsyncClient) -> None:
    account = await register(client)
    text = "When,What,How much\n10/13/2026,Tea,20\n"
    guessed = await preview(client, account, text)
    assert guessed["mapping"]["columns"] == {}  # these headers name nothing it knows
    assert guessed["with_problems"] == 1

    mapping = {
        "header_row": 0,
        "columns": {"date": 0, "title": 1, "amount": 2},
        "date_format": "%m/%d/%Y",
    }
    answer = await preview(client, account, text, mapping)
    assert brief(answer["rows"][0]) == (2, "2026-10-13", "expense", "Tea", 2000)


@pytest.mark.parametrize(
    "mapping",
    [
        {"header_row": 0, "columns": {"title": 1, "amount": 2}, "date_format": "%Y-%m-%d"},
        {"header_row": 0, "columns": {"date": 0, "title": 1}, "date_format": "%Y-%m-%d"},
        {"header_row": 0, "columns": {"date": 0, "title": 1, "amount": 2}, "date_format": "%s"},
        {
            "header_row": 0,
            "columns": {"date": -1, "title": 1, "amount": 2},
            "date_format": "%Y-%m-%d",
        },
    ],
)
async def test_a_mapping_must_say_enough_to_make_entries(
    client: AsyncClient, mapping: dict[str, Any]
) -> None:
    account = await register(client)
    response = await client.post(
        PREVIEW, headers=account.headers, json={"file": encoded("a,b,c\n"), "mapping": mapping}
    )
    assert response.status_code == 422, response.text


async def test_files_rows_under_the_users_categories_by_name(client: AsyncClient) -> None:
    account = await register(client)
    await client.post(
        CATEGORIES, headers=account.headers, json={"name": "Pets", "icon": "🐶", "accent": "teal"}
    )
    text = (
        "date,title,amount,category\n"
        "2026-10-01,Bone,50,PETS\n"
        "2026-10-01,Dosa,60,Food & drinks\n"
        "2026-10-01,Rice,70,groceries\n"
    )
    answer = await preview(client, account, text)
    categories = [
        (row["entry"]["category_id"], row["entry"]["category_guessed"]) for row in answer["rows"]
    ]
    assert categories[1:] == [("other_expense", True), ("groceries", False)]
    assert categories[0][1] is False
    assert answer["unknown_categories"] == ["Food & drinks"]


async def test_keeps_a_long_narration_whole_in_the_note(client: AsyncClient) -> None:
    account = await register(client)
    narration = "UPI/" + "1234567890/" * 15
    answer = await preview(client, account, f"date,title,amount\n2026-10-01,{narration},20\n")
    entry = answer["rows"][0]["entry"]
    assert len(entry["title"]) == 120
    assert entry["title"].endswith("…")
    assert entry["note"] == narration


async def test_reads_a_file_saved_by_a_spreadsheet_in_windows_encoding(client: AsyncClient) -> None:
    account = await register(client)
    answer = await preview(
        client, account, "date,title,amount\n2026-10-01,Café,20\n".encode("cp1252")
    )
    assert answer["rows"][0]["entry"]["title"] == "Café"


async def test_a_file_too_long_is_refused(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = import_rules()
    small = rules.model_copy(
        update={"limits": rules.limits.model_copy(update={"max_rows": 1, "max_bytes": 60})}
    )
    monkeypatch.setattr("app.modules.expenses.features.imports.service.import_rules", lambda: small)
    account = await register(client)
    rows = await client.post(
        PREVIEW,
        headers=account.headers,
        json={"file": encoded("date,title,amount\n2026-10-01,A,1\n2026-10-02,B,2\n")},
    )
    assert (rows.status_code, rows.json()["code"], rows.json()["details"]) == (
        422,
        "EXPENSE_IMPORT_TOO_LARGE",
        {"max_rows": 1},
    )
    size = await client.post(PREVIEW, headers=account.headers, json={"file": encoded("x" * 61)})
    assert (size.status_code, size.json()["details"]) == (422, {"max_bytes": 60})


async def test_only_base64_is_a_file(client: AsyncClient) -> None:
    account = await register(client)
    response = await client.post(PREVIEW, headers=account.headers, json={"file": "not base64!"})
    assert response.status_code == 422


async def test_needs_a_signed_in_user(client: AsyncClient) -> None:
    assert (await client.post(PREVIEW, json={"file": encoded("a")})).status_code == 401


def test_the_apis_body_limit_fits_the_largest_file() -> None:
    """API_MAX_BODY_BYTES refuses bodies before any route: it must take the largest file, sent
    base64-encoded (a third bigger), with its mapping."""
    largest_file = import_rules().limits.max_bytes
    assert ApiSettings(_env_file=None).max_body_bytes >= largest_file * 4 // 3 + ENVELOPE_BYTES
