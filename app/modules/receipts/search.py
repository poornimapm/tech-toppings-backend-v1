"""What the command palette finds in receipts: receipts by what was bought, the shop, the bill's
or the thing's number, or the note, latest bought first."""

from __future__ import annotations

import datetime as dt

from app.modules.receipts.service import ReceiptsItems
from app.platform import ActorContext, PageParams, SearchHit, search


@search
async def receipts_search(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    found, _ = await ReceiptsItems(ctx).page(PageParams(1, limit), None, text=text)
    return [
        SearchHit(
            kind="receipt",
            id=str(item.id),
            title=item.text,
            path=str(item.id),
            amount_minor=item.amount_minor,
            currency=item.currency,
            date=dt.date.fromisoformat(item.bought_on),
        )
        for item in found
    ]
