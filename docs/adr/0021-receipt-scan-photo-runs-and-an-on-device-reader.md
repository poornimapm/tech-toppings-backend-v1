# ADR-0021: Receipt scan: photo runs, and an on-device reader

- **Status:** Accepted
- **Date:** 2026-10-29
- **Deciders:** project owner (Phase 14)

## Context

Phase 14 (U10) turns a photo of a receipt into draft expenses on the same confirm card as
anything said or typed. It's done when extraction accuracy is reported on 20 sample receipts,
no image is persisted anywhere (tested), and the fallback works with AI consent off.

The plan (§7, *Receipts*): `POST /receipts/scan`; Gemini reads the photo in memory with
consent; without consent or quota, Tesseract.js reads it in the browser and the text goes to
the usual capture.

The owner chose (2026-10-29):
- one draft per item, plus a *Combine into one* button on the card;
- with AI on, the photo itself may go to the AI service, with a per-device switch to read
  photos on the device only;
- the on-device reader's files from the app's own site, not a CDN;
- 20 synthetic, photo-like sample receipts (12 English, 4 Tamil, 4 Hindi).

## Decision

### Photos are an input of the agent runtime, not a feature's own AI path

- A skill says in its SKILL.md metadata what it reads besides typed words (`reads: ocr
  photo`) and how much room its answer needs (`max-output-tokens`, used when above the
  provider's own `max_tokens`: one call drafting a 40-line receipt is long).
- `POST /v1/agents/{name}/photo-runs?skill=...&locale=...` takes the photo as the request
  body (no form encoding, so nothing spills to disk), at most `AGENT_MAX_PHOTO_BYTES` (which
  must fit in `API_MAX_BODY_BYTES`, checked at startup), of `AGENT_PHOTO_TYPES`, its type taken
  from its bytes. The agent and skill are checked first (404), then that the skill reads photos
  (422), and that its module is on for the user (403: a switched-off module's skill never
  runs, text or photo).
- `RunRequest.photos` carries it through the runtime. The user's message becomes OpenAI-style
  image parts, sent only to providers marked `vision: true`. With none, or all of them resting
  after a 429 (no quota is spent then), the run is blocked and the switches decide (ADR-0013).
  `GET /v1/agents` says, per skill that reads photos, where a photo run would go now
  (`photo_mode`), so the app never uploads a photo for nothing.
- A run blocked before it produced anything (only reads so far, as when a scan's first call
  fetched the categories and the quota or provider ran out) lets the switches decide, like
  one blocked before its first call: a scan then answers `scan-receipt.on_device`. A run that
  had produced something keeps it. A provider's answer cut off inside a tool call (length
  limit) counts as a failed answer, so the next provider is asked.
- The run stores how many photos it read (`AgentRun.photos`), never the photo. `Photo`'s repr
  hides its bytes. Tests search every collection and the logs for the photo after a run that
  succeeds and after ones that fail (a refusal echoing the image, a 429 mid-run, the quota).
- Static scripts read text only: a photo run that falls to static mode answers
  `scan-receipt.on_device`, and the app reads the photo itself. `RunContext.input_type`
  `photo` is set by the server only.

### The `scan-receipt` skill (expenses)

- `SKILL.md` tells the model to call `expenses_categories` and `expenses_habits`, then
  `expenses_draft` once, one draft per item line, the shop on each, and to make the drafts
  add up to the amount paid (tax summaries under the items are usually already in the
  prices).
- Its static script answers only when asked by name (the scan), so the general assistant
  never reads a sentence as a receipt. It runs the **receipt reader**
  (`parsing/receipt.py`; words in `references/receipt.yaml`; the shared expenses words from
  track-expenses): items are lines ending in figures; payment lines (checked before totals),
  totals, taxes and charges (or any line with a rate, "2.5%"), discounts, subtotals, codes and
  contact lines are told apart by words in en/ta/hi; English words match whole, Tamil and
  Hindi ones at a word's start (short ones whole: कुल is not कुल्फी); the paid total is the last
  total line; the shop is the first naming line, tidied; dates are read day first, never in
  the future or long ago, and a date line, a postal code or a bill number is never an item;
  a column label after an item ("MRP") is cut off; counts come from a count column, a price
  column or a weight (1.500 kg), never from a price. OCR slips are allowed for: look-alike
  letters in prices (`8O.0O`), edge specks (`|`, `770.00 eel`), stray counts after a price.
- `expenses_draft` takes up to 40 drafts (`MAX_ENTRY_DRAFTS`); the reader keeps 38 item lines,
  then one *Rest of the receipt* draft, leaving room for one that balances the total.
- Text read on the device goes to the usual runs endpoint with `input_type: "ocr"`, limited
  by `AGENT_MAX_OCR_CHARS` rather than the typed-input limit, for a skill that reads `ocr`
  only.

### On the device: Tesseract.js, self-hosted, with the photo made ready first

- A Vite plugin (`vite-plugins/ocr-files.ts`) publishes the reader in `ocr/<hash>/` (a hash
  of the files' bytes, as they come from four separately versioned packages): the worker, the
  engine with and without SIMD (script and WebAssembly), and the English, Tamil and Hindi data
  (about 12 MB on the host). The service worker doesn't keep them; they are fetched on the
  first scan without AI, the language data then kept by the reader under that folder's name.
  They are cached a day, then revalidated. `VITE_OCR_PATH` can move them within the site only
  (the reader runs as a worker, which browsers load only from the same site). The content
  policy allows `'wasm-unsafe-eval'` (not `eval`); no third party is involved.
- The reader never waits forever: a load failure the library would swallow ends the read
  (its error handler), and `VITE_SCAN_READ_TIMEOUT_MS` bounds both starting and reading. The
  app then says the reader couldn't start, or that a connection is needed.
- `receipt-image.ts` (since Phase 15 `src/core/scan/photo-image.ts`, ADR-0022) prepares the photo (pure, so the measurement uses the same code), in a
  worker so the page stays alive: grey; cropped to the paper and the table reaching the edges
  whitened, cut just above the table's grey (read from the photo's border), so a shadow on the
  paper stays paper; straightened (a projection profile of the print finds the tilt; with too
  little print, or no clearly better angle, it stays straight); light evened (each pixel
  divided by its block's paper brightness); then the paper's own shadow on the table, dark
  beside what was cleared, whitened too (Tesseract takes a dark frame for a picture and skips
  the print inside). Then Tesseract reads it as one block (page mode 6). Measured on the
  samples, this is what made the reader usable: without it, tilted or shadowed photos lost
  most of their lines.
- The photo for AI is redrawn as a JPEG of `VITE_SCAN_PHOTO_PX` (default 1600 px), which also
  drops its metadata; the one read on the device uses `VITE_SCAN_READ_PX` (2400 px).
- Nothing about the photo is kept on the device: it lives in memory for the scan; the drafts
  (as text) are what the card keeps. Offline, a scan says it needs a connection; the photo is
  not queued.

### Measuring it

- `tests/modules/expenses/receipts/cases.json` holds the 20 labelled receipts; the frontend's
  `npm run receipt-samples render` draws them as photos (Playwright's Chromium) and
  `npm run receipt-samples ocr` reads them as the app does (shrunk to `VITE_SCAN_READ_PX`, the
  same preparation and settings), writing `ocr/<id>.txt`. It runs Tesseract.js in Node, whose
  engine build (relaxed SIMD where Node has it) may differ a little from the browser's.
- `poe eval-receipts` scores the rules on that text (or on the printed text, `--paper`), and
  `--ai` scores a vision provider on the photos with the app's prompt, context, skill, tools
  and their checks (drafts the app would refuse are refused). CI gates the rules at the
  measured floors, drafts included. Results: docs/modules/expenses.md.

## Consequences

- Any skill can now take a photo (a medicine label, a document's expiry date) with no new AI
  path.
- The AI path sends a receipt unmasked: the consent text and the scan's own note say so, and
  the device switch keeps photos local.
- AI accuracy on photos is not yet measured (no key where this was built); the rules' accuracy
  on the OCR text is: fair in English, good in Tamil, poor in Hindi, where OCR drops digits.
- The free AI tier may keep what it receives: the scan's note says this app doesn't keep the
  photo and that the AI service's own terms apply.
- The samples are synthetic; real photos will score lower until they are measured.

## Deviations from the plan (D26)

- `POST /v1/agents/{name}/photo-runs` (with `skill=scan-receipt`) instead of
  `POST /receipts/scan`: photos are a runtime input any skill can use, and the receipts
  module is still *coming soon*.
- The on-device text goes to `POST /v1/agents/{name}/runs` with the `scan-receipt` skill and
  `input_type: "ocr"`, not `capture/interpret` (which became the agents runs endpoint in
  Phase 4).
- Tesseract.js files are self-hosted, not fetched from its default CDN.
