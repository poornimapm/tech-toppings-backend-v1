"""Property tests of the ExpenseQuery compiler (plan, Phase 9b: "the compiler property tests
pass"). Queries are drawn at random from every metric, grouping, order and filter, with a fixed
seed so a failure always comes back the same, and checked two ways:

* the shape: every pipeline keeps only the caller's own live entries in their currency, uses
  only whitelisted stages and operators, caps its rows, and never turns the user's words into
  field names or operators;
* the answers: run on real MongoDB over random entries, each query gives what a plain Python
  reading of the same entries gives.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from httpx import AsyncClient

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.analytics.service import tuning
from app.modules.expenses.features.entries.models import in_base
from app.modules.expenses.features.queries.compiler import STAGES, compile_query
from app.modules.expenses.features.queries.schemas import (
    ExpenseQuery,
    GroupBy,
    Metric,
    SortOrder,
)
from app.modules.expenses.features.queries.service import whole
from tests.helpers import register
from tests.modules.expenses.conftest import add, listed

SEED = 20261016
SHAPE_CASES = 500
ANSWER_CASES = 200
ENTRIES = 40
OWN = {"user_id": "the-caller", "deleted_at": None}
OPERATORS = STAGES | {
    "$sum", "$avg", "$max", "$in", "$regex", "$options", "$gte", "$lte", "$or", "$and",
    "$substrBytes", "$ifNull",
}  # fmt: skip
# Words a user might type to break out of a value, and ordinary ones.
WORDS = ["Big Bazaar", "amma", "goa", "Rice", "$where", '{"$gt": ""}', ".*", "a|b", "(x", "^Tea$"]
EXPENSE_CATEGORIES = ["groceries", "food", "transport"]
MERCHANTS = ["Big Bazaar", "Metro", "Corner Shop", None]
PEOPLE = ["Amma", "Ravi", None]
TAGS = ["home", "goa", "work"]
MODES = ["cash", "upi", None]
FIRST_DAY = dt.date(2026, 9, 1)
DAYS = 60


def queries(rng: random.Random, count: int, words: Sequence[str]) -> Iterator[ExpenseQuery]:
    """Random queries over every option; filters drawn from ``words`` (some match the data)."""
    most = tuning().query.max_rows
    for _ in range(count):
        start = FIRST_DAY + dt.timedelta(days=rng.randrange(DAYS)) if rng.random() < 0.5 else None
        end = (
            (start or FIRST_DAY) + dt.timedelta(days=rng.randrange(DAYS))
            if rng.random() < 0.5
            else None
        )
        low = rng.randrange(0, 50000) if rng.random() < 0.2 else None
        yield ExpenseQuery(
            metric=rng.choice(list(Metric)),
            kind=rng.choice([EntryKind.EXPENSE, EntryKind.INCOME, None]),
            group_by=rng.choice(list(GroupBy)),
            start=start,
            end=end,
            category_ids=rng.sample([*EXPENSE_CATEGORIES, "salary"], rng.randrange(3)),
            merchant=rng.choice([*words, None, None, None]) if rng.random() < 0.3 else None,
            person=rng.choice([*words, None, None]) if rng.random() < 0.2 else None,
            tag=rng.choice([*words, None]) if rng.random() < 0.2 else None,
            text=rng.choice([*words, None]) if rng.random() < 0.2 else None,
            min_amount_minor=low,
            max_amount_minor=(low or 0) + rng.randrange(1, 80000) if rng.random() < 0.2 else None,
            sort=rng.choice(list(SortOrder)),
            limit=rng.randrange(1, most + 1),
        )


def keys(value: Any) -> Iterator[str]:
    """Every dict key in a pipeline, all the way down."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


def test_every_pipeline_reads_only_the_callers_entries_in_fixed_shapes() -> None:
    rng = random.Random(SEED)  # noqa: S311 - a seeded draw for a test, not for security
    for query in queries(rng, SHAPE_CASES, WORDS):
        pipeline = compile_query(query, OWN, "INR")
        first = pipeline[0]["$match"]
        assert {name: first[name] for name in OWN} == OWN, query  # own entries, always
        assert first["$and"] == in_base("INR")["$and"]  # counted in their currency
        assert all(len(stage) == 1 and next(iter(stage)) in STAGES for stage in pipeline)
        operators = {key for key in keys(pipeline) if key.startswith("$")}
        assert operators <= OPERATORS, (query, operators - OPERATORS)
        # The user's words are values: never keys of the pipeline.
        typed = {query.merchant, query.person, query.tag, query.text, *query.category_ids}
        assert not typed & set(keys(pipeline)) - {None}, query
        assert pipeline[-1] == {"$limit": query.limit}
        assert query.limit <= tuning().query.max_rows


def test_the_users_words_are_matched_as_text() -> None:
    for word in WORDS:
        match = compile_query(ExpenseQuery(merchant=word, text=word), OWN, "INR")[0]["$match"]
        exact = match["merchant"]["$regex"]
        assert re.fullmatch(exact, word) is not None
        assert re.fullmatch(exact, f"{word}x") is None  # exactly, not a prefix
        loose = match["$or"][0]["title"]["$regex"]
        assert re.search(loose, f"before {word} after") is not None
        assert re.search(loose, "nothing like it") is None


# ------------------------------------------------------------------ answers on MongoDB


def oracle(entries: Sequence[Mapping[str, Any]], query: ExpenseQuery) -> list[tuple[Any, ...]]:
    """What the query asks, read off the entries in plain Python."""

    def same(value: str | None, wanted: str) -> bool:
        return value is not None and value.casefold() == wanted.casefold()

    def kept(entry: Mapping[str, Any]) -> bool:
        checks = [
            query.kind is None or entry["kind"] == query.kind,
            query.start is None or entry["date"] >= query.start.isoformat(),
            query.end is None or entry["date"] <= query.end.isoformat(),
            not query.category_ids or entry["category_id"] in query.category_ids,
            query.merchant is None or same(entry["merchant"], query.merchant),
            query.person is None or same(entry["for_whom"], query.person),
            query.tag is None or query.tag.casefold() in entry["tags"],
            query.min_amount_minor is None or entry["amount_minor"] >= query.min_amount_minor,
            query.max_amount_minor is None or entry["amount_minor"] <= query.max_amount_minor,
            query.text is None
            or any(
                query.text.casefold() in (entry[name] or "").casefold()
                for name in ("title", "merchant", "note")
            ),
        ]
        return all(checks)

    groups: dict[Any, list[int]] = defaultdict(list)
    for entry in filter(kept, entries):
        if query.group_by is GroupBy.TAG:
            for tag in entry["tags"]:
                if query.tag is None or tag == query.tag.casefold():
                    groups[tag].append(entry["amount_minor"])
            continue
        key = {
            GroupBy.NONE: None,
            GroupBy.CATEGORY: entry["category_id"],
            GroupBy.MERCHANT: entry["merchant"],
            GroupBy.PERSON: entry["for_whom"],
            GroupBy.DAY: entry["date"],
            GroupBy.MONTH: entry["date"][:7],
            GroupBy.KIND: entry["kind"],
            GroupBy.PAYMENT_MODE: entry["payment_mode"],
        }[query.group_by]
        groups[key].append(entry["amount_minor"])

    def value(amounts: list[int]) -> int:
        return {
            Metric.TOTAL: sum(amounts),
            Metric.COUNT: len(amounts),
            Metric.AVERAGE: whole(sum(amounts) / len(amounts)),
            Metric.LARGEST: max(amounts),
        }[query.metric]

    def order(key: Any) -> tuple[int, str]:
        return (0, "") if key is None else (1, key)  # MongoDB puts null first

    rows = [(key, value(amounts), len(amounts)) for key, amounts in groups.items()]
    if query.sort is SortOrder.VALUE_DESC:
        rows.sort(key=lambda row: (-row[1], order(row[0])))
    elif query.sort is SortOrder.VALUE_ASC:
        rows.sort(key=lambda row: (row[1], order(row[0])))
    else:
        rows.sort(key=lambda row: order(row[0]))
    return rows[: query.limit]


async def test_every_answer_is_what_the_entries_say(client: AsyncClient) -> None:
    rng = random.Random(SEED)  # noqa: S311 - a seeded draw for a test, not for security
    account = await register(client)
    for _ in range(ENTRIES):
        income = rng.random() < 0.15
        await add(
            client,
            account,
            kind="income" if income else "expense",
            title=rng.choice(["Rice", "Tea", "Bus", "Salary", "Big Bazaar run"]),
            amount_minor=rng.randrange(1, 100000),
            category_id="salary" if income else rng.choice(EXPENSE_CATEGORIES),
            date=(FIRST_DAY + dt.timedelta(days=rng.randrange(DAYS))).isoformat(),
            merchant=rng.choice(MERCHANTS),
            for_whom=rng.choice(PEOPLE),
            tags=rng.sample(TAGS, rng.randrange(3)),
            payment_mode=rng.choice(MODES),
            note=rng.choice([None, "for goa trip", "monthly"]),
        )
    entries = (await listed(client, account, "&from=2026-01-01"))["items"]
    assert len(entries) == ENTRIES

    words = ["Big Bazaar", "metro", "Amma", "goa", "home", "tea", "monthly", ".*"]
    answered = 0
    for query in queries(rng, ANSWER_CASES, words):
        response = await client.post(
            "/v1/expenses/query",
            headers=account.headers,
            json=query.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        answer = [(row["key"], row["value"], row["count"]) for row in response.json()["rows"]]
        assert answer == oracle(entries, query), query
        answered += bool(answer)
    # Not only empty answers agreeing: most queries find something.
    assert answered >= ANSWER_CASES // 3, answered
