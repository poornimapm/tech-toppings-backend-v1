"""What the command palette finds in Travel: trips by their title, latest first."""

from __future__ import annotations

import datetime as dt

from app.modules.travel.trips import Trips
from app.platform import ActorContext, PageParams, SearchHit, search


@search
async def travel_search(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    found, _ = await Trips(ctx).page(PageParams(1, limit), None, text=text)
    return [
        SearchHit(
            kind="trip",
            id=str(trip.id),
            title=trip.title,
            path=f"trips/{trip.id}",
            date=dt.date.fromisoformat(trip.start_date),
        )
        for trip in found
    ]
