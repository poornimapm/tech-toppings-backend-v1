# ADR-0022: Receipts & warranty, on a shared scan

- **Status:** Accepted
- **Date:** 2026-10-30
- **Deciders:** project owner (Phase 15)

## Context

The owner chose Receipts & warranty as Phase 15 (2026-10-29): the first of the upcoming modules,
made with `poe new-module` and grown, reusing the receipt scan of Phase 14 (ADR-0021). The
roadmap's first slice: receipts with a warranty end date and a reminder before it.

Two things stood in the way of reuse:
- the receipt reader (lines, figures, totals, the shop, the day) lived in the expenses module,
  and modules may not import each other (import-linter);
- the on-device reader (photo preparation, Tesseract.js, the scan's flow and button) lived in
  the expenses frontend module, which another module may not import either (ESLint).

The approved plan also says receipt images are never stored (plan §349 and its risk table).

## Decision

### The scan is shared

- **`app/platform/slips/`** reads a printed slip as OCR gives it: `read_slip(text, today, *,
  max_age_days, max_amount)` returns its lines, item lines (with names), totals, what was added
  (taxes, charges), the shop and the day. Its words (en/ta/hi: totals, payments, taxes, codes,
  dates, months) are data in `words.yaml` beside it; `word_pattern` matches a module's own words
  the same way (English whole, Tamil and Hindi as stems). How old a date and how large a figure
  may be is the caller's: a grocery receipt is from this year and small; a fridge's bill, kept
  for its warranty, may be years old and large.
- Expenses' receipt reader is now what expenses makes of a slip (drafts, quantities, balancing
  to the total), with only its own words in `scan-receipt/references/receipt.yaml`. Measured on
  the 20 samples, it reads exactly as before.
- `western_digits` and `on_or_before`, which both use, moved to the platform API.
- **`src/core/scan/`** holds the on-device reader (`photo-reader.ts`, `photo-image.ts`, the
  preparing worker, the OCR settings), the scan's flow (`usePhotoScan(skill)`: AI for the photo
  where the skill's `photo_mode` says so, else the device) and its button (`ScanPhoto`, worded by
  the module: what it reads). The device switch (*Read photos on this device only*) is one for
  every scan (`tt-scan-on-device`). Words that name no kind of paper moved to `common.json`.

### The receipts module

- Made by `poe new-module` from the coming-soon manifests, then grown. A receipt (`receipts_items`)
  is what was bought, the shop, the day bought (never after today), the amount (in the user's
  base currency then), the bill's number, the thing's serial number or IMEI, a note, and the
  warranty's last day with how many days before it to remind (`data/warranty.yaml`: the default,
  the most, the longest warranty, the lengths offered with one tap).
- **Where a warranty stands** is worked out for the user's today, never stored: none, active,
  ending (from its reminder day to its last day, which it covers), ended. Lists filter by it in
  the database through a stored `remind_on` (the end less the reminder days).
- **A reminder** per receipt (`Reminders.schedule`, ADR-0012's exactly-once delivery): created,
  moved or cancelled with the receipt; none for a warranty already over.
- **Found** by the command palette (`search.py`: name, shop, numbers, note) and the list's own
  search; the dashboard lists the warranties ending soon (`widgets.py`); the Welcome tile counts
  receipts; changes go to the activity feed.
- **The scan fills the form**: the `scan-bill` skill reads a bill (`reads: ocr photo`) into one
  receipt through the `receipts_draft` client tool. Without AI its static script uses the slip
  reader plus a bill's own words (`references/bill.yaml`): the dearest item is what was bought
  (marked to check when there were several), the total paid, the bill's and the thing's numbers
  after their labels, and a printed warranty in months (the first printed, as a thing's own comes
  before a part's longer one; never guessed). The assistant can hand a bill over too: it opens
  a new receipt, filled in, taken once (a reload or Back doesn't fill it again).
- **No photo is kept**, as the plan says: a receipt is a text record. The scan's photo is
  handled as in ADR-0021.

## Consequences

- A future module that reads paper (medicine bills, government documents) starts from
  `read_slip` and `ScanPhoto`.
- Keeping a bill's photo (useful for a warranty claim) would change the plan's privacy promise:
  it is the owner's decision, and would build on the private file store Phase 16 adds for
  worker photos.
- The bill reader isn't measured on samples yet (Phase 14's are grocery receipts); its tests
  cover the shapes it reads.
