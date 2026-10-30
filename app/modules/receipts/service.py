"""The module's rules, between its routes and its data: one user's receipts, and only theirs,
each with a reminder before its warranty ends."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, ClassVar

from fastapi import status

from app.modules.receipts.models import ReceiptsItem, ReceiptsItemRepository
from app.modules.receipts.schemas import ReceiptsItemIn
from app.modules.receipts.warranty import WarrantyStatus, criteria_for, months_after, rules
from app.platform import ActivityLog, ActorContext, AppError, PageParams, Reminders, parse_sort

MODULE = "receipts"
REMINDER = f"{MODULE}.warranty_ends"
SORT_FIELDS = ("created_at", "bought_on", "warranty_ends_on", "text")
NEWEST = "-bought_on"
SEARCHED = ("text", "shop", "bill_number", "serial_number", "note")
KEPT_AS_SENT = {"text", "shop", "amount_minor", "bill_number", "serial_number", "note"}


class ReceiptDateInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "RECEIPT_DATE_INVALID"
    default_message = "A receipt can't be dated after today."


class WarrantyInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "RECEIPT_WARRANTY_INVALID"
    default_message = (
        "A warranty ends on or after the day of buying, and within the longest warranty taken."
    )


def _like(text: str) -> dict[str, Any]:
    return {"$regex": re.escape(text), "$options": "i"}


class ReceiptsItems:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.today = ctx.today()
        self.repository = ReceiptsItemRepository(ctx.scope, ctx.clock)
        self.reminders = Reminders(ctx)
        self.activity = ActivityLog(ctx.scope, ctx.clock, ctx.settings)

    def _fields(self, body: ReceiptsItemIn) -> dict[str, Any]:
        """The record as stored, checked against the user's today: never bought later, and a
        warranty that ends from the day of buying up to the longest taken."""
        bought = body.bought_on or self.today
        if bought > self.today:
            raise ReceiptDateInvalidError
        ends, warranty = body.warranty_ends_on, rules().warranty
        if ends is not None and not bought <= ends <= months_after(bought, warranty.max_months):
            raise WarrantyInvalidError
        days = body.remind_days_before
        if days is None:
            days = warranty.remind_days_before
        return {
            **body.model_dump(include=KEPT_AS_SENT),
            "bought_on": bought.isoformat(),
            "warranty_ends_on": ends.isoformat() if ends else None,
            "remind_days_before": days,
            "remind_on": (ends - dt.timedelta(days=days)).isoformat() if ends else None,
        }

    async def _remind(self, item: ReceiptsItem) -> None:
        """One reminder per receipt, before its warranty ends; none once it has ended."""
        ref = str(item.id)
        ends = item.warranty_ends_on
        if ends is None or ends < self.today.isoformat():
            await self.reminders.cancel(module=MODULE, ref_id=ref)
            return
        await self.reminders.schedule(
            REMINDER,
            module=MODULE,
            ref_id=ref,
            due_on=dt.date.fromisoformat(ends),
            days_before=item.remind_days_before,
            title=item.text,
            receipt_id=ref,
        )

    async def add(self, body: ReceiptsItemIn) -> ReceiptsItem:
        fields = {**self._fields(body), "currency": self.ctx.user.base_currency}
        item = await self.repository.create(fields)
        await self._remind(item)
        await self.activity.record(f"{MODULE}.item_added", module=MODULE, title=item.text)
        return item

    async def page(
        self,
        params: PageParams,
        sort: str | None,
        warranty: WarrantyStatus | None = None,
        text: str | None = None,
    ) -> tuple[list[ReceiptsItem], int]:
        criteria: dict[str, Any] = criteria_for(warranty, self.today) if warranty else {}
        if text:
            criteria["$or"] = [{name: _like(text)} for name in SEARCHED]
        # Ties (the same day, the same instant) in one fixed order, so pages never overlap.
        order = [*parse_sort(sort, SORT_FIELDS, NEWEST), ("_id", -1)]
        return await self.repository.list(params, order, criteria)

    async def ending(self, limit: int) -> list[ReceiptsItem]:
        """Warranties ending soon, the soonest first."""
        found, _ = await self.page(PageParams(1, limit), "warranty_ends_on", WarrantyStatus.ENDING)
        return found

    async def get(self, item_id: str) -> ReceiptsItem:
        return await self.repository.get(item_id)

    async def change(self, item_id: str, body: ReceiptsItemIn) -> ReceiptsItem:
        item = await self.repository.update(item_id, self._fields(body))
        await self._remind(item)
        await self.activity.record(f"{MODULE}.item_updated", module=MODULE, title=item.text)
        return item

    async def delete(self, item_id: str) -> None:
        item = await self.repository.get(item_id)
        await self.repository.soft_delete(item_id)
        await self.reminders.cancel(module=MODULE, ref_id=str(item.id))
        await self.activity.record(f"{MODULE}.item_deleted", module=MODULE, title=item.text)
