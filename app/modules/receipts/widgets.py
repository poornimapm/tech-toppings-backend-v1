"""Dashboard widgets: the warranties ending soon, whatever the period shown."""

from __future__ import annotations

from app.modules.receipts.service import MODULE, REMINDER, ReceiptsItems
from app.modules.receipts.warranty import rules
from app.platform import ActorContext, DateRange, Item, Items, widget


@widget("list")
async def receipts_warranties_ending(
    ctx: ActorContext,
    period: DateRange,  # noqa: ARG001 - the warranties ending from today, whatever the period
) -> Items:
    found = await ReceiptsItems(ctx).ending(rules().dashboard.max_items)
    return Items(
        items=[
            Item(
                code=REMINDER,  # worded as the reminder is
                params={"module": MODULE, "title": item.text, "due": item.warranty_ends_on},
            )
            for item in found
        ]
    )
