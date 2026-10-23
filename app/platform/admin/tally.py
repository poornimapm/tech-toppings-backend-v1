"""How many times each error code was answered, per UTC day, for the admin's usage page. The
error handlers call ``app.state.tally(code)``: it only counts in memory, and the counts are
written in one batch at most every ``ADMIN_ERROR_TALLY_FLUSH_SECONDS`` (and at shutdown), so a
flood of failing requests costs one write per interval, never one per answer. Counting never
fails or slows the answer it counts."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections import Counter

from beanie import Document
from pymongo import ASCENDING, IndexModel, UpdateOne

from app.core.clock import Clock
from app.core.config import AdminSettings
from app.core.logging import get_logger

logger = get_logger("app.admin")


class ErrorTally(Document):
    day: str  # YYYY-MM-DD (UTC)
    code: str
    times: int = 0
    purge_at: dt.datetime

    class Settings:
        name = "error_tallies"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("day", ASCENDING), ("code", ASCENDING)], unique=True, name="day_code"),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]


class ErrorCounter:
    """What ``app.state.tally`` is: count one answer of ``code`` today."""

    def __init__(self, clock: Clock, settings: AdminSettings) -> None:
        self.clock = clock
        self.settings = settings
        self.counts: Counter[tuple[str, str]] = Counter()
        self._writing: asyncio.Task[None] | None = None

    def __call__(self, code: str) -> None:
        self.counts[(self.clock.now().date().isoformat(), code)] += 1
        self._schedule()

    def _schedule(self) -> None:
        if self._writing is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: nothing to write with
            return
        self._writing = loop.create_task(self._write_soon())

    async def _write_soon(self) -> None:
        try:
            await asyncio.sleep(self.settings.error_tally_flush_seconds)
            await self.flush()
        finally:
            self._writing = None
            if self.counts:  # counted while writing
                self._schedule()

    async def flush(self) -> None:
        """Write what has been counted (also called at shutdown)."""
        counts, self.counts = self.counts, Counter()
        if not counts:
            return
        purge_at = self.clock.now() + dt.timedelta(days=self.settings.error_tally_days)
        writes = [
            UpdateOne(
                {"day": day, "code": code},
                {"$inc": {"times": times}, "$setOnInsert": {"purge_at": purge_at}},
                upsert=True,
            )
            for (day, code), times in counts.items()
        ]
        try:
            await ErrorTally.get_pymongo_collection().bulk_write(writes, ordered=False)
        except Exception as exc:  # the database may be the error being counted
            logger.debug("error_tally_skipped", codes=len(writes), error_type=type(exc).__name__)

    async def close(self) -> None:
        """At shutdown: stop waiting and write what is left."""
        if self._writing is not None:
            self._writing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writing
        await self.flush()


def error_tally(clock: Clock, settings: AdminSettings) -> ErrorCounter:
    return ErrorCounter(clock, settings)
