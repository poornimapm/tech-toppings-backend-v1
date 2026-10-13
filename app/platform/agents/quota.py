"""Free tiers have daily caps: count AI calls per user and globally, per UTC day."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.core.clock import Clock
from app.core.config import AgentSettings
from app.platform.agents.models import AgentUsage


class Quota:
    def __init__(self, settings: AgentSettings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock

    def _keys(self, user_id: PydanticObjectId) -> tuple[tuple[str, int], tuple[str, int]]:
        day = self._clock.now().astimezone(UTC).date().isoformat()
        return (
            (f"{day}:user:{user_id}", self._settings.user_daily_calls),
            (f"{day}:global", self._settings.global_daily_calls),
        )

    async def remaining(self, user_id: PydanticObjectId) -> int:
        counts = []
        for key, limit in self._keys(user_id):
            usage = await AgentUsage.find_one({"key": key})
            counts.append(limit - (usage.calls if usage else 0))
        return max(0, min(counts))

    async def _take(self, key: str, limit: int) -> bool:
        if limit <= 0:  # a zero budget switches AI off without touching the counters
            return False
        day_start = datetime.combine(self._clock.now().astimezone(UTC).date(), time(), UTC)
        try:
            await AgentUsage.get_pymongo_collection().update_one(
                {"key": key, "calls": {"$lt": limit}},
                {"$inc": {"calls": 1}, "$setOnInsert": {"purge_at": day_start + timedelta(days=2)}},
                upsert=True,
            )
        except DuplicateKeyError:  # the counter exists and is at its limit
            return False
        return True

    async def take(self, user_id: PydanticObjectId) -> bool:
        """Use one call from both budgets, or neither."""
        (user_key, user_limit), (global_key, global_limit) = self._keys(user_id)
        if not await self._take(user_key, user_limit):
            return False
        if await self._take(global_key, global_limit):
            return True
        await AgentUsage.get_pymongo_collection().update_one(
            {"key": user_key}, {"$inc": {"calls": -1}}
        )
        return False
