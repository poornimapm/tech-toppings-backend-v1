"""The test module's daily job. It only counts its runs per user, so tests can see how often
the platform ran it."""

from __future__ import annotations

from collections import Counter

from app.platform import ActorContext, job

RUNS: Counter[str] = Counter()


@job("daily")
async def notes_tick(ctx: ActorContext) -> int:
    RUNS[str(ctx.user.id)] += 1
    return 1
