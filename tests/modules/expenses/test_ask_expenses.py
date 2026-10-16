"""The ask-expenses skill without AI (static mode): questions about the user's own figures in
English, Tamil and Hindi, read into an ExpenseQuery and answered with the skill's templates."""

from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.platform.plugins import load_script
from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import add, travel
from tests.platform.agents.conftest import AgentClient, ask

STATIC = Path(__file__).parents[3] / "app/modules/expenses/skills/ask-expenses/scripts/static.py"
# Thursday 8 October 2026, 11:30 in Kolkata: "last month" is September.
THURSDAY = datetime(2026, 10, 8, 6, tzinfo=UTC)


async def september(client: AsyncClient, clock: FrozenClock) -> Account:
    account = await register(client)
    travel(clock, THURSDAY)
    for title, amount, merchant in (("Rice", 100000, "Big Bazaar"), ("Dal", 50000, "Big Bazaar")):
        await add(
            client, account, title=title, amount_minor=amount, merchant=merchant, date="2026-09-05"
        )
    await add(
        client,
        account,
        title="Metro card",
        amount_minor=30000,
        category_id="transport",
        merchant="Metro",
        date="2026-09-10",
    )
    await add(
        client,
        account,
        title="Salary",
        kind="income",
        category_id="salary",
        amount_minor=5000000,
        date="2026-09-01",
    )
    return account


SEPTEMBER = {"currency": "INR", "from_on": "2026-09-01", "to_on": "2026-09-30"}


@pytest.mark.parametrize(
    ("question", "code", "figures"),
    [
        ("How much did I spend on groceries last month?", "ask-expenses.total.expense",
         {"amount_minor": 150000, "count": 2}),
        ("how much did I spend last month", "ask-expenses.total.expense",
         {"amount_minor": 180000, "count": 3}),
        ("How many times did I spend money last month?", "ask-expenses.count.expense",
         {"amount_minor": None, "count": 3}),
        ("what was my average expense last month", "ask-expenses.average.expense",
         {"amount_minor": 60000, "count": 3}),
        ("biggest expense last month", "ask-expenses.largest.expense",
         {"amount_minor": 100000, "count": 3}),
        ("How much did I earn last month?", "ask-expenses.total.income",
         {"amount_minor": 5000000, "count": 1}),
        ("போன மாதம் எவ்வளவு செலவு செய்தேன்?", "ask-expenses.total.expense",
         {"amount_minor": 180000, "count": 3}),
        ("पिछले महीने कितना खर्च हुआ?", "ask-expenses.total.expense",
         {"amount_minor": 180000, "count": 3}),
    ],
)  # fmt: skip
async def test_answers_with_the_users_own_figures(
    agent_client: AgentClient,
    clock: FrozenClock,
    question: str,
    code: str,
    figures: dict[str, Any],
) -> None:
    async with agent_client(mode="static") as client:
        account = await september(client, clock)
        run = await ask(client, account, question)
    assert (run["skill"], run["message_code"]) == ("ask-expenses", code)
    assert run["message_params"] == SEPTEMBER | figures


async def test_names_where_most_of_it_went(agent_client: AgentClient, clock: FrozenClock) -> None:
    async with agent_client(mode="static") as client:
        account = await september(client, clock)
        run = await ask(client, account, "Where did I spend the most last month?")
    assert run["message_code"] == "ask-expenses.top.2"
    assert run["message_params"] == SEPTEMBER | {
        "first_name": "Big Bazaar",
        "first_minor": 150000,
        "second_name": "Metro",
        "second_minor": 30000,
    }


async def test_says_when_nothing_is_there(agent_client: AgentClient, clock: FrozenClock) -> None:
    async with agent_client(mode="static") as client:
        account = await september(client, clock)
        run = await ask(client, account, "how much did I spend yesterday")
    assert run["message_code"] == "ask-expenses.none"
    assert run["message_params"] == {
        "currency": "INR",
        "from_on": "2026-10-07",
        "to_on": "2026-10-07",
    }


@pytest.mark.parametrize("words", ["open expenses", "rice 50", "pin habits"])
async def test_leaves_what_is_not_a_question_to_other_skills(
    agent_client: AgentClient, clock: FrozenClock, words: str
) -> None:
    async with agent_client(mode="static") as client:
        account = await september(client, clock)
        run = await ask(client, account, words)
    assert run["skill"] != "ask-expenses"


def test_period_words_mean_whole_weeks_and_months() -> None:
    script = load_script(STATIC, "ask_expenses_static_for_test")
    today = dt.date(2026, 10, 8)  # a Thursday
    assert script.days("last_week", today) == (dt.date(2026, 9, 28), dt.date(2026, 10, 4))
    assert script.days("this_week", today) == (dt.date(2026, 10, 5), today)
    assert script.days("last_month", dt.date(2026, 3, 31)) == (
        dt.date(2026, 2, 1),
        dt.date(2026, 2, 28),
    )
    assert script.days("this_year", today) == (dt.date(2026, 1, 1), today)
