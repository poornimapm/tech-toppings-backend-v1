"""The test module's dashboard widgets: one of each kind, as a real module would write them."""

from __future__ import annotations

from app.platform import (
    NEWEST_FIRST,
    ActorContext,
    DateRange,
    Item,
    Items,
    Kpi,
    PageParams,
    Series,
    count_in,
    daily_counts,
    widget,
)
from tests.fixtures.modules.notes.models import Note, NoteRepository


@widget("kpi")
async def notes_created(ctx: ActorContext, period: DateRange) -> Kpi:
    criteria = NoteRepository(ctx.scope, ctx.clock).query()
    return Kpi(
        value=await count_in(Note, criteria, period),
        previous=await count_in(Note, criteria, period.previous()),
    )


@widget("series")
async def notes_per_day(ctx: ActorContext, period: DateRange) -> Series:
    return Series(
        points=await daily_counts(Note, NoteRepository(ctx.scope, ctx.clock).query(), period)
    )


@widget("list")
async def notes_latest(ctx: ActorContext, period: DateRange) -> Items:
    notes, _ = await NoteRepository(ctx.scope, ctx.clock).list(
        PageParams(1, 5), NEWEST_FIRST, period.match()
    )
    return Items(items=[Item(title=note.text, at=note.created_at) for note in notes])
