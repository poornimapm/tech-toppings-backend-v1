"""The Welcome tile's figure: how many receipts are kept."""

from __future__ import annotations

from app.modules.receipts.models import ReceiptsItem, ReceiptsItemRepository
from app.platform import Clock, Scope, TileStat


async def receipt_count(scope: Scope, clock: Clock) -> TileStat:
    count = await ReceiptsItem.find(ReceiptsItemRepository(scope, clock).query()).count()
    return TileStat(value=count, label="items")
