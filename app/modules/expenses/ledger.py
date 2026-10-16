"""Money ledgers kept inside a revisioned record: a goal's contributions, a loan's payments. Each
line carries the key its request was sent with, so a retried request adds it once. Each line is
one revision of the record that holds that line alone (the repository's ``ledgers``), so the
history shows every line and grows with their number."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from typing import Annotated, Any, ClassVar, TypeVar

from pydantic import AfterValidator, BaseModel, Field

from app.modules.expenses.planning import planning
from app.platform import (
    ConflictError,
    RevisionedDocument,
    RevisionedRepository,
    VersionConflictError,
)

MAX_MINOR = 10**13  # the largest amount taken, as entries take it


def _not_zero(amount: int) -> int:
    if amount == 0:
        raise ValueError("an amount of 0 changes nothing")
    return amount


# What a line moves: negative to take money back out, or to correct an earlier line.
LedgerAmount = Annotated[int, Field(ge=-MAX_MINOR, le=MAX_MINOR), AfterValidator(_not_zero)]


class LedgerItem(BaseModel):
    """One line of a ledger. Only JSON values (no datetime), as revisions need."""

    key: str  # the Idempotency-Key it was sent with, or one the server made up


class LedgerFullError(ConflictError):
    code: ClassVar[str] = "EXPENSE_LEDGER_FULL"
    default_message = "This is full: it can't take more money in or out. Start a new one."


TRecord = TypeVar("TRecord", bound=RevisionedDocument)
TItem = TypeVar("TItem", bound=LedgerItem)


async def append_once(
    repo: RevisionedRepository[TRecord],
    entity_id: str,
    field: str,
    item: TItem,
    *,
    check: Callable[[TRecord, TItem], Mapping[str, Any]],
    source: str,
) -> tuple[TRecord, bool]:
    """Add ``item`` to the record's ``field`` list. The second value says whether it was added
    now: a line with the same key already there makes this a replay, and nothing changes.

    ``check`` sees the record as it stands, refuses the line by raising an ``AppError``, and
    returns the fields that follow from it (a new total). A record holds at most
    ``ledger.max_lines`` lines. A write that loses a race to another write of the record is
    tried again on the record as it then is, after a random pause that widens each time, so
    every total stays exact. After ``ledger.retries`` lost races the answer is 409
    ``VERSION_CONFLICT``; sending the request again with the same key is safe."""
    rules = planning().ledger
    attempts = 0
    while True:
        current = await repo.get(entity_id)
        lines: list[TItem] = getattr(current, field)
        if any(line.key == item.key for line in lines):
            return current, False
        if len(lines) >= rules.max_lines:
            raise LedgerFullError(details={"max_lines": rules.max_lines})
        changes = check(current, item)
        try:
            updated = await repo.append(
                entity_id, field, item, changes, expected=current.revision_no, source=source
            )
        except VersionConflictError:
            attempts += 1
            if attempts > rules.retries:
                raise
            # Writes that lost together would only race again at once: spread them out, wider
            # each time.
            pause = random.uniform(0, rules.pause_ms * 2**attempts)  # noqa: S311 - not secret
            await asyncio.sleep(pause / 1000)
        else:
            return updated, True
