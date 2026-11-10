"""The figure on the module's Welcome tile: how many trips are still to come or under way, each by
its own today."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.modules.travel.models import Trip, TripPhase, TripRepository
from app.modules.travel.trips import in_phase
from app.platform import Clock, Scope, TileStat, User


async def trips_ahead(scope: Scope, clock: Clock) -> TileStat | None:
    user = await User.get(scope.owner_id)
    if user is None:
        return None
    repository, now = TripRepository(scope, clock), clock.now()
    today = now.astimezone(ZoneInfo(user.timezone)).date()
    phases = [
        await in_phase(repository, phase, now, today)
        for phase in (TripPhase.UPCOMING, TripPhase.ACTIVE)
    ]
    count = await Trip.find(repository.query({"$or": phases})).count()
    return TileStat(value=count, label="items")
