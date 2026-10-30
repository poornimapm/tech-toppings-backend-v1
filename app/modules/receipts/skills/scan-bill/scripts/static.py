"""Static mode for scan-bill: a bill's text, read on the device, through the bill reader. A
photo can't be read without AI: the app is told to read it on the device first."""

from __future__ import annotations

from app.modules.receipts.bill import load_bill, read_bill
from app.platform import StaticOutcome, StaticRequest, ToolBox


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    if not request.chosen:
        return None  # only the scan asks: any other text is the other skills' to read
    if not request.input.strip():
        return StaticOutcome("scan-bill.on_device")
    words = load_bill(request.references)
    bill = read_bill(request.input, request.today, words)
    if bill is None:
        return StaticOutcome("scan-bill.unreadable")
    await tools.client(words.tool, {"draft": bill.arguments()})
    total = {"total": float(bill.amount)} if bill.amount is not None else {}
    return StaticOutcome("scan-bill.ready", total)
