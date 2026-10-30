"""Agent tools for receipts. The assistant never keeps anything itself: it hands over a receipt
read from a bill (``receipts_draft``), which the user checks and saves through the API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.receipts.warranty import rules
from app.platform import ActorContext, tool

DAY_TEXT = r"^\d{4}-\d{2}-\d{2}$"  # a day as the model writes it; checked again when saved
Field_ = Literal[
    "text", "shop", "bought_on", "amount", "bill_number", "serial_number", "warranty_months"
]


class BillDraftIn(BaseModel):
    """A receipt as read from a bill. Leave out what can't be read."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str | None, Field(max_length=200, description="The thing bought, as printed")
    ] = None
    shop: Annotated[str | None, Field(max_length=80, description="The shop's name")] = None
    bought_on: Annotated[
        str | None, Field(pattern=DAY_TEXT, description="The bill's date, YYYY-MM-DD")
    ] = None
    amount: Annotated[
        float | None, Field(gt=0, description="The total paid, in the currency's main unit")
    ] = None
    bill_number: Annotated[str | None, Field(max_length=40)] = None
    serial_number: Annotated[str | None, Field(max_length=60)] = None
    warranty_months: Annotated[
        int | None,
        Field(
            ge=1,
            le=rules().warranty.max_months,
            description="The warranty's length as printed, in months; leave out if none",
        ),
    ] = None
    uncertain: Annotated[
        list[Field_], Field(description="The fields guessed or not clearly read")
    ] = []


@tool("client")
async def receipts_draft(ctx: ActorContext, draft: BillDraftIn) -> None:
    """Show the user a receipt to check and keep, filled in from a bill. Nothing is kept until
    they save it."""
