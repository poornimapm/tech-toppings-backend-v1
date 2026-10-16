"""Loans through the assistant (U2): "lent Ravi 500" becomes a loan for the user to check and
save, never an expense and never a write. Static mode takes only explicit loan words; agent
mode is a (simulated) AI provider using the same tools."""

from __future__ import annotations

import json
from typing import Any

import respx
from httpx import AsyncClient

from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import listed
from tests.modules.expenses.test_capture import drafts_of
from tests.modules.expenses.test_figures import today_of
from tests.platform.agents.conftest import (
    LLM_URL,
    PRIMARY,
    AgentClient,
    ask,
    calls,
    give_consent,
    reply,
    sent,
)

SKILL = "track-expenses"
SAID = {"skill": SKILL, "context": {"input_type": "speech"}}  # spoken on the Expenses page
IOUS = "/v1/expenses/ious"
UNSAID = {"date": None, "due_on": None, "note": None, "iou_id": None}


def loans_of(run: dict[str, Any]) -> list[dict[str, Any]]:
    (action,) = run["client_actions"]
    assert action["tool"] == "expenses_iou_draft"
    return list(action["arguments"]["drafts"])


async def stored(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    page = (await client.get(f"{IOUS}?state=all", headers=account.headers)).json()
    return list(page["items"])


async def lend(
    client: AsyncClient, account: Account, person: str = "Ravi", *, settled: bool = False
) -> str:
    """A loan of 1000 to the person (paid back in full when ``settled``); its id."""
    made = await client.post(
        IOUS,
        headers=account.headers,
        json={"counterparty": person, "direction": "lent", "amount_minor": 100000},
    )
    if settled:
        await client.post(
            f"{IOUS}/{made.json()['id']}/settlements",
            headers=account.headers,
            json={"amount_minor": 100000},
        )
    return str(made.json()["id"])


async def test_a_loan_said_in_any_language_becomes_a_loan_to_check(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        lent = await ask(client, account, "lent Ravi 500", **SAID)
        hindi = await ask(client, account, "रवि से 2000 उधार लिया", **SAID)
        tamil = await ask(client, account, "ரவிக்கு 500 கடன் கொடுத்தேன்", **SAID)
        anywhere = await ask(client, account, "lent Ravi 500")  # from the general assistant
        entries, loans = await listed(client, account), await stored(client, account)

    assert (lent["mode"], lent["skill"], lent["message_code"]) == (
        "static",
        SKILL,
        "track-expenses.ready.iou",
    )
    # The name is picked out of the sentence, so it is marked for the user to check.
    assert loans_of(lent) == [
        {
            "action": "new",
            "direction": "lent",
            "counterparty": "Ravi",
            "amount": 500.0,
            "uncertain": ["counterparty"],
        }
        | UNSAID
    ]
    assert [(d["direction"], d["counterparty"], d["amount"]) for d in loans_of(hindi)] == [
        ("borrowed", "रवि", 2000.0)
    ]
    assert [(d["direction"], d["counterparty"], d["amount"]) for d in loans_of(tamil)] == [
        ("lent", "ரவி", 500.0)
    ]
    assert (anywhere["skill"], anywhere["message_code"]) == (SKILL, "track-expenses.ready.iou")
    # Nothing is written: the user saves each loan.
    assert (lent["proposals"], entries["total"], loans) == ([], 0, [])


async def test_being_paid_back_is_a_repayment(
    agent_client: AgentClient, clock: FrozenClock
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        back = await ask(client, account, "Ravi paid back 500 yesterday", **SAID)
        mine = await ask(client, account, "paid back 1000 to Meena", **SAID)

    yesterday = today_of(clock).fromordinal(today_of(clock).toordinal() - 1).isoformat()
    # Paid back to the user, most likely: the words don't say, so that is marked too.
    assert loans_of(back) == [
        {
            "action": "repay",
            "direction": "lent",
            "counterparty": "Ravi",
            "amount": 500.0,
            "uncertain": ["direction", "counterparty"],
        }
        | UNSAID
        | {"date": yesterday}
    ]
    assert [(d["action"], d["direction"], d["counterparty"]) for d in loans_of(mine)] == [
        ("repay", "borrowed", "Meena")
    ]


async def test_money_given_without_a_loan_word_stays_an_expense(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        watchman = await ask(client, account, "gave 200 to the watchman", **SAID)
        papa = await ask(client, account, "पापा को 5000 भेजे", **SAID)
        auto = await ask(client, account, "I came back home and paid 200 for auto", **SAID)

    assert [(d["kind"], d["title"], d["amount"]) for d in drafts_of(watchman)] == [
        ("expense", "Watchman", 200.0)
    ]
    assert watchman["message_code"] == "track-expenses.ready.draft"
    assert [d["amount"] for d in drafts_of(papa)] == [5000.0]
    assert [d["amount"] for d in drafts_of(auto)] == [200.0]


async def test_being_paid_back_counts_only_on_the_expenses_page(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        later = await ask(client, account, "I'll be back in 10 minutes")
        paid = await ask(client, account, "Ravi paid back 500")

    assert later["skill"] != SKILL
    # It speaks of money, so it is an entry to check, not a payment on a loan.
    assert (paid["message_code"], [d["amount"] for d in drafts_of(paid)]) == (
        "track-expenses.ready.draft",
        [500.0],
    )


# ------------------------------------------------------------------ with AI


LENT = {"direction": "lent", "counterparty": "Ravi", "amount": 500}
BORROWED = {"direction": "borrowed"}


@respx.mock
async def test_ai_looks_up_the_loan_and_hands_over_a_repayment(
    agent_client: AgentClient, clock: FrozenClock
) -> None:
    async with agent_client(providers=[PRIMARY]) as client:
        account = await register(client)
        await give_consent(client, account)
        made = await lend(client, account)
        await lend(client, account, "Meena")  # someone else
        await lend(client, account, settled=True)  # paid back already
        repay = LENT | {"action": "repay", "iou_id": made}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("expenses_ious", {"person": "ravi"})),
                calls(("expenses_iou_draft", {"drafts": [repay]})),
                reply("Ravi's payment is ready for you to save."),
            ]
        )
        run = await ask(client, account, "Ravi paid me back 500", **SAID)
        loans = await stored(client, account)

    offered = {tool["function"]["name"] for tool in sent(route, 0)["tools"]}
    looked_up = [m["content"] for m in sent(route, 1)["messages"] if m["role"] == "tool"]
    assert {"expenses_ious", "expenses_iou_draft"} <= offered
    # Only Ravi's open loan: not Meena's, nor the one paid back.
    assert json.loads(looked_up[0])["result"] == [
        {
            "id": made,
            "counterparty": "Ravi",
            "direction": "lent",
            "amount": 1000.0,
            "settled": 0.0,
            "outstanding": 1000.0,
            "currency": "INR",
            "date": today_of(clock).isoformat(),
            "due_on": None,
            "settled_on": None,
        }
    ]
    assert loans_of(run) == [
        repay | {"amount": 500.0, "date": None, "due_on": None, "note": None, "uncertain": []}
    ]
    # Handed over, not written: the loan is as it was until the user saves the payment.
    assert (run["proposals"], sorted(loan["settled_minor"] for loan in loans)) == (
        [],
        [0, 0, 100000],
    )


@respx.mock
async def test_loan_arguments_that_do_not_fit_go_back_to_the_ai(
    agent_client: AgentClient,
) -> None:
    async with agent_client(providers=[PRIMARY]) as client:
        account = await register(client)
        await give_consent(client, account)
        other = await lend(client, await register(client))
        mine = await lend(client, account)
        paid_up = await lend(client, account, settled=True)
        repay = LENT | {"action": "repay"}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(
                    ("expenses_iou_draft", {"drafts": [LENT | {"direction": "gifted"}]}),
                    ("expenses_iou_draft", {"drafts": [LENT | {"amount": -5}]}),
                    ("expenses_iou_draft", {"drafts": [repay | {"iou_id": "0" * 24}]}),
                    ("expenses_iou_draft", {"drafts": [repay | {"iou_id": other}]}),
                    ("expenses_iou_draft", {"drafts": [LENT | {"iou_id": mine}]}),
                    ("expenses_iou_draft", {"drafts": [repay | {"iou_id": mine} | BORROWED]}),
                    ("expenses_iou_draft", {"drafts": [repay | {"iou_id": paid_up}]}),
                    ("expenses_iou_draft", {"drafts": [LENT | {"owner": "someone else"}]}),
                ),
                calls(("expenses_iou_draft", {"drafts": [LENT]})),
                reply("Prepared."),
            ]
        )
        run = await ask(client, account, "lent Ravi 500", **SAID)

    told = [json.loads(m["content"]) for m in sent(route, 1)["messages"] if m["role"] == "tool"]
    assert [answer["error"] for answer in told] == [
        "invalid_arguments",
        "invalid_arguments",
        "NOT_FOUND",
        "NOT_FOUND",  # another user's loan is no loan at all
        "invalid_arguments",  # only a repayment pays back a loan
        "EXPENSE_SETTLEMENT_INVALID",  # Ravi owes the user, not the other way round
        "EXPENSE_SETTLEMENT_INVALID",  # paid back already
        "invalid_arguments",
    ]
    assert len(loans_of(run)) == 1  # only the call that fit reached the user
