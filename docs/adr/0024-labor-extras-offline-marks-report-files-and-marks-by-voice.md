# ADR-0024: Labor's extras: offline marks, report files and marks by voice

- **Status:** Accepted
- **Date:** 2026-10-30
- **Deciders:** project owner (Phase 17: "Finish Labor's extras")

## Context

Phase 16 left three of the Labor spec's items for later (ADR-0023, `docs/modules/labor.md`):
recording attendance with no network, a report as a file, and marking attendance by voice.
The spec asks for offline support only in the app's own way ("do not introduce a new offline
architecture"), and for exports only "consistently with the current stack". The Phase 16 live
check (an owner and the admin clicking through) also found figures that did not add up and a
few gaps; they are fixed here too.

## Decision

### Offline marks: the outbox, with the mark seen (ADR-0010)

- A tap that fails for want of a network waits in the outbox (`labor.marks`), one item per
  worker, so one refused mark never holds back the others. The shell sends it when back online,
  as it sends expenses. Its sender loads only when such an item waits (first load: one line).
- Each waiting mark carries the mark the owner saw when tapping (`MarkIn.expected`: `full`,
  `half`, `absent` or `unmarked`). The server refuses one whose day was marked otherwise in the
  meantime (409 `LABOR_MARK_CHANGED`, with the mark it now has), before anything is written: an
  old tap never overwrites a newer mark made on another device. The banner shows it, worded, to
  discard. Sent twice, a mark changes nothing more (it already holds).
- A tap saved online drops the taps still waiting for the same worker and day: it overtook them.
- The card shows a waiting tap as pressed with *Waiting to send*; the summary stays what is
  saved, with a line saying how many wait.
- The app keeps no session across a restart without a network (ADR-0003: the token lives in
  memory), so "offline" means a day opened online stays usable when the network drops: its sheet
  stays in memory for 12 hours unseen. A day not opened before says so instead of loading forever.

### Report files: a CSV from the server, a PDF from the browser (D5)

- `GET /v1/labor/projects/{id}/report.csv` (and the admin's copy under `/v1/admin/labor`): the
  report's figures (`total`, `period`, `worker` rows with what is owed to date), then each day
  marked (`mark`) and each payment not voided (`payment`), one header with a `section` column,
  like the expenses monthly file. Built whole: a period is at most 400 days; a file of more than
  `reports.export_max_rows` days and payments is refused before it is made
  (`LABOR_EXPORT_TOO_LARGE`). The filename has dates only (ASCII).
- The CSV rules (a byte-order mark, CRLF, the formula guard) move from expenses to
  `app/core/csv_file.py`, shared through `app.platform`: one way to write a CSV.
- *Print or save as PDF* prints the Reports page: the frame, filters and buttons don't print; a
  title says the project and the days; tables print whole with their captions as titles.

### Marks by voice: a skill that drafts, the owner saves (ADR-0013)

- `mark-attendance` (`app/modules/labor/skills/`): "Ravi full day, Meena half day, the rest
  absent", said or typed, in English, Tamil or Hindi.
  - **Static mode** reads it by rules (`marks_reader.py`, the words in `attendance.yaml`):
    workers by name, first name or number; each mark word goes with the names before it, or
    after it when a sentence starts with one; "the rest"; the day said. The longer of two
    overlapping words counts ("नहीं आया" over "आया"). A name close to a worker's, one matching
    no one, and a name without a mark are handed over marked uncertain.
  - **Agent mode** reads the day's workers (`labor_day`) and hands over the same draft; it can
    match a name heard in another script.
  - Either way the draft goes to the page (`labor_attendance_draft`, a client tool): nothing is
    saved until the owner checks it and taps *Save*. "The rest" covers only workers not named
    and not yet marked.
- The attendance page asks through `POST /v1/labor/projects/{id}/attendance/read?day=`, which
  runs the skill with the page's project and day as its context (`run_skill`), so the platform's
  run API needs no change. From the general assistant it reads only words that say a mark, and
  only for the owner's one open project (with several, it asks which); the draft opens on the
  attendance page (the manifest's `actions`).
- Reading needs the network (the skill runs on the server); tapping doesn't.

### Fixes from the Phase 16 live check

- **Payments added up wrong**: it showed earned and paid since the project's start date, but
  what is owed over every day (days may be recorded before the start date). Report rows now
  carry `earned_to_date_minor` and `paid_to_date_minor` beside `owed_to_date_minor`, and the
  worker's `code` and `name`.
- **Workers past the first page** (20) were missing from Payments and Wages, and nameless in
  Reports: those pages now read every page (`useAllWorkers`), and reports name their workers.
- **The audit named a payment's worker by id**: entries carry `labels` (a worker's name and
  number), read in the reader's own scope.
- **The admin could not open a project**: the admin's view opens one (its workers, whether an
  ID picture is kept, its last 30 days, a CSV), finds projects by name and state, and shows
  every change from and to.
- **The header scrolled sideways in Tamil**: its controls wrap to a second row; the app's name
  shows beside the logo from 1280 px.
- Buttons repeated on every row ("Pay", "Change wage") are named with their worker.

## Consequences

- First-load JavaScript is 199.6 KB of 200: the next module's manifest needs room. The next lever
  is loading English module replies and feed words with the module rather than the first page.
- Offline after a restart would need a session that survives one, which ADR-0003 rules out.
- Static-mode voice matching works within one script; names heard in another script need agent
  mode (with consent) or the owner's choice in the check.
