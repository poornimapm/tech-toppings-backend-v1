"""``ledger.append_once``: a line goes in once per key, a write that loses a race to another
write of the record is tried again on the record as it then is, each line is a revision that
holds that line alone, and a record takes only so many lines."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient
from pydantic import BaseModel

from app.core.repository import Scope
from app.core.revisions import DEFAULT_SOURCE, VersionConflictError
from app.modules.expenses.features.goals.models import (
    Contribution,
    Goal,
    GoalRepository,
    GoalRevision,
)
from app.modules.expenses.ledger import LedgerFullError, append_once
from app.modules.expenses.planning import planning
from app.platform import ConflictError
from tests.helpers import FrozenClock


class Racing(GoalRepository):
    """Another write of 1000 lands between each read and this write, ``races`` times."""

    races = 0

    async def append(
        self,
        entity_id: str | PydanticObjectId,
        field: str,
        line: BaseModel,
        changes: Mapping[str, Any],
        *,
        expected: int,
        source: str = DEFAULT_SOURCE,
    ) -> Goal:
        if self.races:
            self.races -= 1
            current = await self.get(entity_id)
            await self.update(entity_id, {"saved_minor": current.saved_minor + 1000})
        return await super().append(
            entity_id, field, line, changes, expected=expected, source=source
        )


async def bike(repo: GoalRepository) -> str:
    fields = {"name": "Bike", "target_minor": 10000, "currency": "INR", "funding": "manual"}
    return str((await repo.create({**fields, "since": "2026-10-08"})).id)


def line(key: str, amount_minor: int = 100) -> Contribution:
    return Contribution(key=key, amount_minor=amount_minor, date="2026-10-08")


def total(goal: Goal, item: Contribution) -> dict[str, int]:
    return {"saved_minor": goal.saved_minor + item.amount_minor}


async def put(repo: GoalRepository, goal_id: str, key: str) -> tuple[Goal, bool]:
    return await append_once(
        repo, goal_id, "contributions", line(key), check=total, source="manual"
    )


@pytest.fixture
def repo(
    client: AsyncClient,  # noqa: ARG001 - the running app opens the database
    clock: FrozenClock,
) -> Racing:
    return Racing(Scope.for_user(PydanticObjectId()), clock)


async def test_a_line_whose_key_is_in_already_is_a_replay(repo: Racing) -> None:
    goal_id = await bike(repo)

    first, added = await put(repo, goal_id, "k-1")
    again, added_again = await append_once(
        repo, goal_id, "contributions", line("k-1", 999), check=total, source="manual"
    )

    assert (added, added_again) == (True, False)
    assert (first.revision_no, again.revision_no) == (2, 2)
    assert (again.saved_minor, [item.amount_minor for item in again.contributions]) == (100, [100])
    assert again.pending_revision is None


async def test_each_line_is_a_revision_that_holds_that_line_alone(repo: Racing) -> None:
    goal_id = await bike(repo)
    for n in range(1, 4):
        await put(repo, goal_id, f"k-{n}")

    revisions = await GoalRevision.find({"entity_id": PydanticObjectId(goal_id)}).to_list()
    by_number = {revision.revision_no: revision for revision in revisions}

    # The list itself is never copied into history, so it grows with the lines, not their square.
    assert sorted(by_number) == [1, 2, 3, 4]
    assert all("contributions" not in revision.snapshot for revision in revisions)
    last = by_number[4]
    assert [(change.field, change.old, change.new) for change in last.changes] == [
        ("saved_minor", 200, 300),
        ("contributions", None, line("k-3").model_dump(mode="json")),
    ]
    assert last.snapshot["saved_minor"] == 300
    goal = await repo.get(goal_id)
    assert [item.key for item in goal.contributions] == ["k-1", "k-2", "k-3"]


async def test_a_lost_race_is_tried_again_on_the_record_as_it_then_is(repo: Racing) -> None:
    goal_id = await bike(repo)
    repo.races = 2

    goal, added = await put(repo, goal_id, "k-1")

    assert added is True
    # Both writes that got in first count: the total is worked out again on each try.
    assert (goal.saved_minor, goal.revision_no) == (2100, 4)
    assert [item.key for item in goal.contributions] == ["k-1"]


async def test_it_gives_up_after_the_retries_set_in_planning(
    repo: Racing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planning().ledger, "retries", 2)
    goal_id = await bike(repo)
    repo.races = 3  # one more than the retries

    with pytest.raises(VersionConflictError):
        await put(repo, goal_id, "k-1")

    goal = await repo.get(goal_id)
    assert (goal.contributions, goal.saved_minor) == ([], 3000)


async def test_a_full_record_takes_no_more_lines_but_still_replays(
    repo: Racing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planning().ledger, "max_lines", 2)
    goal_id = await bike(repo)
    await put(repo, goal_id, "k-1")
    await put(repo, goal_id, "k-2")

    with pytest.raises(LedgerFullError) as refused:
        await put(repo, goal_id, "k-3")
    replayed, added = await put(repo, goal_id, "k-2")

    assert (refused.value.code, refused.value.details) == ("EXPENSE_LEDGER_FULL", {"max_lines": 2})
    assert (added, replayed.saved_minor, len(replayed.contributions)) == (False, 200, 2)


async def test_a_refused_line_changes_nothing(repo: Racing) -> None:
    goal_id = await bike(repo)

    def refuse(_goal: Goal, _item: Contribution) -> dict[str, int]:
        raise ConflictError

    with pytest.raises(ConflictError):
        await append_once(repo, goal_id, "contributions", line("k-1"), check=refuse, source="x")

    goal = await repo.get(goal_id)
    assert (goal.contributions, goal.revision_no) == ([], 1)


async def test_only_a_ledger_named_by_the_repository_takes_lines(repo: Racing) -> None:
    goal_id = await bike(repo)

    with pytest.raises(ValueError, match="not one of"):
        await repo.append(goal_id, "name", line("k-1"), {}, expected=1)
