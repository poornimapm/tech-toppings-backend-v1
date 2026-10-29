# tech-toppings-backend-v1

This is the backend API for **Tech-Toppings**, a modular, multi-user personal platform. One
shell app hosts independent modules, and Module 1 is a voice-first expense tracker. It is a
FastAPI service on MongoDB, run as a modular monolith.

- Frontend: <https://github.com/poornimapm/tech-toppings-frontend-v1>
- Plan and decisions: [docs/plan.md](docs/plan.md), [docs/adr/](docs/adr/)
- Architecture overview: [docs/architecture.md](docs/architecture.md)

> **Status:** Phase 2 (auth, users, RBAC) is done. Endpoints: health probes, `/v1/auth/*`,
> `/v1/me` and `/v1/me/sessions`.

## Stack

| Concern | Choice |
|---|---|
| Language and runtime | Python 3.11 in a **project-level venv** (`.venv`) |
| Web | FastAPI on uvicorn |
| Database | MongoDB 8 via **PyMongo Async** + **Beanie 2** ODM (local mongod; Atlas M0 in production) |
| Config | pydantic-settings, one prefixed group per concern (see [.env.example](.env.example)) |
| Logging | structlog: JSON to stdout, request IDs, secret redaction |
| Quality | ruff, mypy `--strict`, import-linter, pytest + coverage, pip-audit, gitleaks |
| Tasks | poethepoet (`poe <task>`), cross-platform with no `make` needed |

## Prerequisites

- **Python 3.11**: `py -3.11 --version` on Windows.
- **MongoDB 8.x** reachable at `mongodb://localhost:27017`.
- Optional: Docker Desktop, only for `docker compose up`.

## Setup (first time)

All packages are installed into `.venv` inside this folder. Nothing is installed globally.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements\dev.lock
.venv\Scripts\pip install --no-deps --editable .
copy .env.example .env            # then set AUTH_JWT_SECRET (see below) and adjust values
.venv\Scripts\pre-commit install  # optional: run lint/type checks on every commit
```

On macOS or Linux, use `python3.11` and `.venv/bin/` instead of `.venv\Scripts\`.

Then edit `.env`:

1. `AUTH_JWT_SECRET`: paste the output of
   `.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. `AUTH_ALLOWED_EMAILS`: the emails that may register (comma-separated). Registration is
   allowlist-only by default.
3. `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`: your admin account. Create it
   with `.venv\Scripts\poe seed`.

> **Tip:** you can activate the venv with `.venv\Scripts\Activate.ps1` and then type `poe dev`.
> If PowerShell blocks scripts, call the tools directly, for example `.venv\Scripts\poe dev`.

## Run

```powershell
.venv\Scripts\poe dev
```

This serves the API at <http://127.0.0.1:8000> with auto-reload. `HOST` and `PORT` come from
`.env`.

### Verify

| Check | Expected |
|---|---|
| <http://127.0.0.1:8000/healthz> | `200 {"status":"ok","name":"Tech-Toppings API","version":"0.1.0"}` |
| <http://127.0.0.1:8000/readyz> | `200 {"status":"ready","checks":{"mongo":"ok"}}` |
| `/readyz` with MongoDB stopped | `503 {"code":"SERVICE_NOT_READY",...,"request_id":"..."}`. The app keeps running and recovers by itself when MongoDB is back. |
| <http://127.0.0.1:8000/docs> | Interactive OpenAPI docs |
| Any unknown path | `404 {"code":"NOT_FOUND","message":...,"details":null,"request_id":...}` |

## Accounts and sign-in

| Endpoint | Purpose |
|---|---|
| `GET /v1/auth/config` | Public: registration mode, password policy, Google client id |
| `POST /v1/auth/register` | Allowlisted emails only (`AUTH_REGISTRATION_MODE`). Signs in on success |
| `POST /v1/auth/login` | Email + password. Locks the account with growing backoff after repeated failures |
| `POST /v1/auth/refresh` | Rotates the httpOnly refresh cookie and returns a new access token. Needs the `X-TT-CSRF` header |
| `POST /v1/auth/logout`, `/logout-all` | Sign out this device, or every device (which also invalidates access tokens) |
| `POST /v1/auth/password/change` | Signs out other devices. Required after an admin reset |
| `POST /v1/auth/google` | Optional "Sign in with Google" (set `AUTH_GOOGLE_CLIENT_ID`) |
| `POST /v1/auth/login/totp` | Second step when two-step sign-in is on: the challenge from `/login` (or `/google`) plus a code |
| `GET /v1/auth/totp`, `POST …/setup`, `/enable`, `/disable`, `/recovery-codes` | Two-step sign-in (TOTP): status, set up, turn on and off, new recovery codes |
| `GET/PATCH /v1/me` | Own profile: name, language, theme, currency, time zone, AI consent |
| `GET /v1/me/export`, `DELETE /v1/me` | Everything kept about me as one JSON file; delete my account for good |
| `GET /v1/me/sessions`, `DELETE /v1/me/sessions/{id}` | Signed-in devices; sign one out |

- **Tokens.** Access tokens last 15 minutes and are kept in memory by the frontend. The
  refresh token exists only as a cookie (`HttpOnly; SameSite=Strict`, path `/api/v1/auth`).
  Reusing an old refresh token signs that device out (ADR-0003).
- **No email in v1.** If someone forgets their password, run
  `.venv\Scripts\poe reset-password --email them@example.com`. It prints a one-time
  password once, signs them out everywhere and turns two-step sign-in off; they must choose a
  new password at the next sign-in. The reset is written to the audit trail.
- **Try it:** open `/docs`, call `POST /v1/auth/register` with an allowlisted email, copy the
  `access_token`, click **Authorize**, then call `GET /v1/me`.

Every response carries an `X-Request-ID` header. Send your own (8–128 characters from
`[A-Za-z0-9._-]`) and it is echoed back and included in every log line for that request.

## Scheduled jobs

Render's free plan has no cron and sleeps when idle, so scheduled work takes two paths
(ADR-0007):

- **Lazy (the main path).** Each time the notification bell polls `GET /v1/notifications`,
  the jobs that are due run for that user, each within `JOBS_LAZY_TIMEOUT_SECONDS`. A slow
  or failing job is logged as `job_failed` and never fails the poll.
- **Cron (best effort).** `.github/workflows/jobs-cron.yml` calls
  `POST /v1/internal/jobs/{job}/run` every 30 minutes with the `X-Job-Token` header, for
  `all` jobs or one by name. It catches up users signed in within `JOBS_ACTIVE_DAYS`, most
  recent first, at most `JOBS_MAX_USERS_PER_RUN` per run, and answers with counts only. The
  route is left out of the OpenAPI schema, and while `JOBS_TOKEN` is empty it answers 401 to
  every call.

A package adds jobs in its own `jobs.py`; names start with the package's key:

```python
from app.platform import ActorContext, job


@job("daily")  # or "each": runs on every catch-up, so it must be cheap when nothing is due
async def mymodule_tidy(ctx: ActorContext) -> int:
    ...  # do what is due for ctx.user; make every effect idempotent per item
    return handled
```

- A `daily` job runs once per user and local day. Its lease in `job_runs` lasts
  `JOBS_LEASE_SECONDS` and is also the backoff after a failed run.
- Exactly-once never rests on the lease or the schedule: every effect has its own guard (a
  unique key or a guarded update).
- A module's jobs run only for users who have that module switched on.

**Try it locally.** Put a `JOBS_TOKEN` in `.env` (see `.env.example`), restart `poe dev`,
sign in once in the app, then:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/internal/jobs/all/run `
  -Headers @{ "X-Job-Token" = "<your JOBS_TOKEN>" }
```

It answers `users` (how many accounts it caught up), then `ran`, `skipped`, `failed`, `done`
and `more`. The platform's `reminders_deliver` (an `each` job) runs on every call and counts
the reminders it told in `done`; run it twice and the second answer shows any daily jobs as
skipped. A wrong token gets `401`.

## Planning pack: try it

Each Phase 9a feature can be tried from `/docs` (sign in, then **Authorize** with the access
token). Their numbers are in `app/modules/expenses/data/planning.yaml`.

### Duplicate warning

1. `POST /v1/expenses/entries` with
   `{"title": "Rice", "amount_minor": 12500, "category_id": "groceries", "date": "2026-10-08"}`.
2. `POST /v1/expenses/entries/duplicates/check` with
   `{"candidates": [{"title": "rice ", "amount_minor": 12500, "date": "2026-10-09"}]}`. The
   answer lists that entry under `results[0].matches`.
3. Change the amount, or move the date two days away, and `matches` is empty. Saving the same
   entry again still works: the check is advice only.

An entry matches when it has the same kind and amount, is at most `window_days` away, and has
the same name (ignoring case and spaces), a name at least `similarity` alike ("Tomato" and
"Tomatoes"), or the same place. A call takes up to 10 candidates and returns at most
`max_results` matches for each, newest first.

### Budgets

1. Add a trip's spending with a tag: `POST /v1/expenses/entries` with
   `{"title": "Hotel", "amount_minor": 600000, "category_id": "travel", "date": "<a trip day>", "tags": ["Goa"]}`.
2. `POST /v1/expenses/budgets` with
   `{"name": "Goa trip", "scope": "tag", "tag": "goa", "period": "custom", "start": "<first day>", "end": "<last day>", "limit_minor": 1000000}`,
   choosing a first and last day that include today (a trip that has ended or not started
   never warns). Its `status` says 600000 spent (60%), what an even pace would have spent by today
   (`pace_minor`), and what is left a day (`per_day_left_minor`, for `days_left`).
3. Add another `goa` entry of 200000 on a trip day. That is 80%, so `GET /v1/notifications`
   (the bell) has one `expenses.budget_near`. Save or edit more entries and it stays one; pass
   the limit and one `expenses.budget_over` follows.
4. `GET /v1/expenses/entries/summary?from=<first day>&to=<last day>&tag=goa` totals the same
   `expense_minor` as the budget's `spent_minor`: a budget counts with the list's own filters.

A budget counts all spending (`overall`), one `category_id` or one `tag`, for each `week`
(Monday first) or `month` by the user's own calendar, or between its own dates (`custom`). It
warns once at `alert_threshold` of its limit (default `budgets.alert_threshold`; 0 turns this
off) and once when over, in each week, month or date range, and again after its limit changes.
It warns only while it is under way, and what it has told is kept on the budget, so the
notification's retention never makes it warn twice. A budget counts in the currency it was made
in, even after the user changes their base currency. `PATCH` changes only what is sent. A user
keeps at most `budgets.max_per_user` budgets (`EXPENSE_BUDGET_LIMIT`, 409).

### Goals

1. `POST /v1/expenses/goals` with the header `Idempotency-Key: bike-goal-1` and
   `{"name": "Bike", "target_minor": 5000000, "target_date": "<a day months ahead>"}`. Its
   `per_month_minor` is what is left spread over the months to that date (this one counted).
2. `POST /v1/expenses/goals/<id>/contributions` with the header `Idempotency-Key: bike-add-1` and
   `{"amount_minor": 1200000}`: `saved_minor` is 1200000 and `pct` 24. Send the same request again
   and the answer carries `Idempotent-Replayed: true` with the same total. A negative amount takes
   money out; taking out more than is saved, or dating it after today, is refused
   (`EXPENSE_CONTRIBUTION_INVALID`, 422, with the `reason` in `details`).
3. Add `{"amount_minor": 3800000}` with a new key. The goal is `reached`, and `GET
   /v1/notifications` has one `expenses.goal_reached`. Looking at the goals again tells nothing
   new; raising the target and reaching it again does.
4. `POST /v1/expenses/goals` with `{"name": "Cushion", "target_minor": 10000000, "funding":
   "net_savings", "since": "<the first of this month>"}`. Its `saved_minor` equals
   `total.net_minor` of `GET /v1/expenses/analytics/net?from=<the same day>&to=<today>`: money
   in less money out, never below 0. Add an income entry and it grows; it takes no contributions.

A goal is revisioned like an entry: `PATCH` takes the `revision_no` it edited (409 when the goal
has moved on, for example when money was added meanwhile), `DELETE` keeps it, and
`POST /v1/expenses/goals/<id>/undelete` brings it back. Each contribution is one revision that
records that line alone, so history grows with the number of lines. Contributions that arrive
at the same moment land once each: a write that loses the race is tried again after a random
pause that widens each time, up to `ledger.retries` times. One that still loses answers 409
`VERSION_CONFLICT`, and sending it again with the same key is safe. A goal holds at most
`ledger.max_lines` contributions (`EXPENSE_LEDGER_FULL`, 409).

### Repeating entries and bills

1. An electricity bill that reminds: `POST /v1/expenses/recurring` with
   `{"template": {"title": "Electricity", "amount_minor": 120000, "category_id": "utilities"}, "cadence": {"unit": "month", "anchor": "<a day two days ahead>"}, "mode": "remind", "remind_days_before": 2}`.
   `GET /v1/reminders` lists it, due on that day. It is told two days before, at
   `REMINDERS_SEND_HOUR` your time: today, so at once if that hour has passed.
2. `GET /v1/notifications` (the bell) then has one `expenses.bill_due`. Poll again, or run the
   cron (see [Scheduled jobs](#scheduled-jobs)) as often as you like: still one.
3. `POST /v1/expenses/recurring/<id>/mark-paid` with `{"due_on": "<that day>"}` adds the
   Electricity entry (its `recurring_id` is the rule) and moves the bill on: `next_run` is a
   month later, and the reminder with it. Send it again and the answer carries
   `Idempotent-Replayed: true` with the same entry. A day that isn't a due date, or one after
   `next_run`, is refused (`EXPENSE_RECURRING_NOT_DUE`, 422). Unpaid, a bill stays due:
   `GET /v1/expenses/recurring` shows it `overdue` once the day has passed.
4. Something that adds itself: the same `POST` with `"mode": "auto_create"`, a `"unit":
   "week"` and today as the anchor adds today's entry at once (`source: recurring`), and each
   next one on its day when the bell or the cron catches up.
5. Add three entries named "Milk", a week apart, the last within the week. `GET
   /v1/expenses/recurring/suggestions` offers a weekly rule for it. Confirm it by sending that
   suggestion's `template`, `cadence` and `signature` (with a `mode`) to `POST
   /v1/expenses/recurring`; confirming it twice finds the same rule. Or
   `POST /v1/expenses/recurring/suggestions/dismiss` with its `signature`: it is not suggested
   again.

Each due date is worked out from the anchor, so a month's day is kept (31 Jan, 28 Feb, 31 Mar).
A new rule starts at its first due date from today; nothing before today is added. Each due
date adds one entry at most, however many runs meet: its client key is
`recurring/<rule>/<due date>`. After a long time away, a rule adds at most
`recurring.max_catch_up` entries (the latest). `PATCH` changes only what is sent; a new pace
starts again from today, and so does a paused rule switched back on, or a bill switched to
adding itself, unless a later due date was paid already. Pausing or deleting a bill stops its
reminders, and deleting a rule confirmed from a suggestion dismisses that suggestion. A rule
keeps its category even once archived, and so do the entries it adds. Its amount is in the
currency it was made in: after the base currency changes, its entries are refused
(`EXPENSE_CURRENCY_UNSUPPORTED`), never relabelled.
Suggestions are found afresh from the last `recurring.detect.lookback_days` of your own entries
(not those a rule added): at least `min_occurrences` at a steady pace and amount.

### Loans

1. `POST /v1/expenses/ious` with the header `Idempotency-Key: ravi-loan-1` and
   `{"counterparty": "Ravi", "direction": "lent", "amount_minor": 100000, "due_on": "<tomorrow>"}`.
   Send it again and the answer carries `Idempotent-Replayed: true` with the same loan. A loan is
   never an entry: `GET /v1/expenses/entries` is unchanged.
2. `GET /v1/reminders` lists it, due tomorrow. It is told `ious.remind_days_before` day before, at
   `REMINDERS_SEND_HOUR` your time: today, so at once if that hour has passed. `GET
   /v1/notifications` (the bell) then has one `expenses.iou_due_lent`, however often it polls or
   the cron runs (see [Scheduled jobs](#scheduled-jobs)).
3. `POST /v1/expenses/ious/<id>/settlements` with the header `Idempotency-Key: ravi-pay-1` and
   `{"amount_minor": 40000}`: `outstanding_minor` is 60000. Send it again and nothing more is
   paid. More than is left, below nothing, or dated after today or before the loan is refused
   (`EXPENSE_SETTLEMENT_INVALID`, 422, with the `reason` and `outstanding_minor` in `details`); a
   negative amount corrects a payment.
4. `POST /v1/expenses/ious` with `{"counterparty": "ravi ", "direction": "borrowed",
   "amount_minor": 30000}` and a new key. `GET /v1/expenses/ious/summary` has one person, `ravi`,
   with `owed_to_me_minor` 60000, `i_owe_minor` 30000, `net_minor` 30000 and `next_due_on`
   tomorrow.
5. Pay the rest of the first loan (`{"amount_minor": 60000}`, a new key). It has `settled_on`,
   leaves `GET /v1/expenses/ious` (open ones by default; `?state=settled` or `all`), and its
   reminder is cancelled.

A loan is revisioned like a goal: `PATCH` takes the `revision_no` it edited (409 when the loan has
moved on, for example when a payment came in meanwhile) and never sets the amount below what was
paid back (`EXPENSE_IOU_BELOW_SETTLED`, 422); a new `due_on` moves the reminder. `DELETE` keeps it
and stops its reminder, and `POST /v1/expenses/ious/<id>/undelete` brings it back.
`GET /v1/expenses/ious/<id>` reads one loan (a reminder's link names the loan, not the person).
Payments that arrive at the same moment never take what is paid back past the amount, and each
lands once per key. `?person=` lists one person's loans by name as compared ("Ravi" and "ravi "
are one person). The summary counts the loans in the user's base currency.

### Loans by voice

With AI off (static mode), or on:

1. `POST /v1/agents/assistant/runs` with
   `{"input": "lent Ravi 500", "skill": "track-expenses", "context": {"input_type": "text"}}`.
   The answer has `message_code` `track-expenses.ready.iou` and one client action,
   `expenses_iou_draft`, with `{"action": "new", "direction": "lent", "counterparty": "Ravi",
   "amount": 500.0, "uncertain": ["counterparty"]}`. Nothing is saved: `GET /v1/expenses/ious`
   is still empty.
2. The app saves what the user checked: `POST /v1/expenses/ious` with the header
   `Idempotency-Key: ravi-1` and `{"counterparty": "Ravi", "direction": "lent", "amount_minor":
   50000}`. `GET /v1/expenses/ious/summary` has `owed_to_me_minor` 50000.
3. Ask again with `"input": "Ravi paid me back 200"`: a `repay` draft for a loan you lent.
   Saving it is `POST /v1/expenses/ious/<id>/settlements` with a new key and
   `{"amount_minor": 20000}`; the loan's `outstanding_minor` is then 30000.

Only explicit loan words count (`references/ious.yaml` of the track-expenses skill): "gave
Ravi 500" is an expense, a refund or change from a shop is never a loan, and a sentence that
mixes a loan with spending ("lent Ravi 500 and paid 300 for lunch") stays entries in static
mode. Being paid back is read only when asked from the Expenses page (with `input_type`): from
the general assistant, "I'll be back in 10 minutes" is not a payment. Each part of a sentence is
its own loan, and the direction is marked for checking when the words don't say who gave ("Ravi
paid back 500"). With AI on, the model looks the loan up with `expenses_ious` and hands its
`iou_id` over; a draft that doesn't fit that loan (the other way round, or paid back already)
goes back to the model as `EXPENSE_SETTLEMENT_INVALID`.

## Data pack: try it

Each Phase 9b feature can be tried from `/docs` in the same way. Their numbers are in
`app/modules/expenses/data/listing.yaml`.

### Saved views

1. `POST /v1/expenses/saved-filters` with
   `{"name": "Pets last month", "view": {"show": "expense", "period": "last_month", "category_id": "<a category id>"}}`.
   `GET /v1/expenses/saved-filters` lists it, with every filter spelled out.
2. A period is kept by name (`this_month`, `last_month`, `last_90_days`, `all_time`), so the
   view always shows that period as of the day it is opened. Days a chart chose are kept as
   `from` and `to` instead; giving both a period and days is refused (422), as is anything the
   list can't filter by.
3. The same name again, in any case, answers 409 `EXPENSE_SAVED_FILTER_EXISTS`.
   `PATCH /v1/expenses/saved-filters/<id>` with `{"name": ...}` renames it, or with
   `{"view": ...}` replaces its filters. `DELETE` frees the name.

A user keeps at most `saved_filters.max_per_user` views (`EXPENSE_SAVED_FILTER_LIMIT`, 409).
Opening a view is the app's work: it puts the filters back into the list's URL.

### Bulk edit

1. Add two expenses and one income entry. `POST /v1/expenses/entries/bulk-update` with
   `{"ids": ["<expense 1>", "<expense 2>", "<income>"], "action": "category", "category_id": "food"}`.
   The answer has `changed` (the two expenses) and `skipped` with the income entry and the
   reason `kind`: food is a category for expenses.
2. `GET /v1/expenses/entries/<expense 1>/revisions`: the newest revision changed `category_id`,
   with the source `bulk`. Budgets and prices see the change as they see an edit, so moving
   entries into a budget's category can warn.
3. `{"ids": [...], "action": "add_tag", "tag": "goa"}` and `"remove_tag"` add or take off a tag;
   `"delete"` deletes, and `"undelete"` brings back (how the app undoes a bulk delete).
   `GET /v1/activity` notes each change once ("Changed 2 entries"), or as an edit of that entry
   when it changed one.

Other reasons to skip an entry: `not_found` (not one of yours), `unchanged` (already so),
`conflict` (edited at the same moment; try again) and `tags_full`. A change takes at most
`bulk.max_entries` entries; each is saved on its own, so some can go through while others are
skipped.

### Search

1. With a saved view, a loan, a product and an entry whose names have "goa" in them (or a
   place or note, for an entry), `GET /v1/search?q=goa`. The answer has one group,
   `expenses`, with a fair share for each kind: the view (`path` `?view=<id>`), the loan
   (`plan?show=loans&focus=<id>`, with what is still owed and when it is due), the product
   (`prices/<id>`) and the entry (`entries/<id>`, with its amount and day).
2. `q=g` finds nothing (shorter than `SEARCH_MIN_CHARS`); what is typed is matched as text,
   any case, so `(5 kg` finds "Rice (5 kg)".
3. Switch Expenses off (`PATCH /v1/modules/expenses/preferences {"enabled": false}`) and its
   group is gone.

### CSV export

1. `GET /v1/expenses/export.csv?from=2026-10-01&to=2026-10-31` downloads
   `expenses-2026-10-01-to-2026-10-31.csv`: the entries the list shows for those filters (any
   of the list's own: `kind`, `category_id`, `tag`, `q`, `deleted` ...), newest first.
2. It is UTF-8 with a byte-order mark, so Excel and LibreOffice show Tamil and Hindi right. The
   columns are `date, time, kind, title, amount, currency, category, quantity, unit, merchant,
   payment_mode, for_whom, tags, note`; amounts are in the main unit with every decimal
   (`125.50`), tags are joined with `;`, and a category is your own name for it or the system
   key (`groceries`).
3. Text a spreadsheet would run as a formula (`=`, `+`, `-`, `@` first) starts with `'`.
4. More than `export.max_rows` entries answers 422 `EXPENSE_EXPORT_TOO_LARGE` before anything
   is sent; the file itself is streamed, so memory stays small. The route has its own rate
   limit (`RATE_LIMIT_EXPORT`).

### CSV import: preview

1. Base64-encode a CSV file (PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("statement.csv"))`)
   and `POST /v1/expenses/import/preview` with `{"file": "<base64>"}`. Nothing is saved.
2. The answer guesses the `mapping`: the header row (a bank statement's own lines above it are
   skipped), which column holds what (from the words in `data/imports.yaml`, in English, Tamil
   and Hindi; this app's export maps itself), the date format, and whether a single amount
   column uses minus for money spent (`signs`).
3. `rows` are the first rows as they would be saved, each with `problems` (`bad_date`,
   `missing_amount`, `currency` ...) or `duplicate: true` when an entry with the same day, kind,
   amount and title is saved already; `problems` lists the rows a problem stops, and the counts
   say how many rows the import would add. A category the file names is matched to yours by
   name (or a system key); others are filed under Other and listed in `unknown_categories`.
4. Send the file again with a changed `mapping` (`header_row`, `columns` by index,
   `date_format` from `date_formats`, `signs`) to read it that way.

### CSV import: commit

1. `POST /v1/expenses/import/commit` with the same `file`, the `mapping` the preview showed,
   and `offset` 0. It adds up to `limits.commit_batch` of the importable rows (those with no
   problem, and not saved already unless `include_duplicates` is true) and answers `added`,
   `already`, `done`, `total` and `next_offset`.
2. Send it again with `offset` = `next_offset` until that is `null`. The entries have the source
   `import`, and `GET /v1/activity` notes the import once, when its last batch adds rows.
3. Send the same file again, or a batch whose reply was lost: nothing is added twice
   (`already`). Each row is keyed by the file's fingerprint and its row number.

The server keeps nothing: each request sends the file and mapping again.

### Monthly report

1. `GET /v1/expenses/reports/monthly?month=2026-09`: the month's `summary` (spent, received,
   kept, against the same days of the month before), `categories` with what was spent under
   each the month before and the change, `income`, `merchants`, `people`, money spent each
   day (`days`) and the biggest expenses (`largest`). Leave out `month` for this month so far.
2. `GET /v1/expenses/reports/monthly.csv?month=2026-09` downloads
   `expenses-report-2026-09.csv`: a row per figure (`section, name, amount, share_pct,
   previous, change_pct`), UTF-8 with a byte-order mark.
3. The PDF is the browser's "Save as PDF" on the app's report page (D5), so Tamil and Hindi
   are shaped right without fonts on the server.

### Chat with expenses

1. With AI off (static mode) or on: `POST /v1/agents/assistant/runs` with
   `{"input": "How much did I spend on groceries last month?"}`. The `ask-expenses` skill
   answers `ask-expenses.total.expense` with `amount_minor`, `from_on` and `to_on` (the whole
   month before), worded by the app. "Where did I spend the most this week?" names the top
   places (`ask-expenses.top.<n>`); "how many times", "average", "biggest" and "earn" work too,
   in English, Tamil and Hindi.
2. The figures come from `POST /v1/expenses/query`: an `ExpenseQuery` (metric, kind, grouping,
   days, categories, place, person, tag, words, amounts, order and limit) compiled from a
   whitelist into a pipeline that only ever reads your own entries (the AI calls the same
   compiler through the `expenses_query` tool). Try
   `{"metric": "total", "group_by": "month", "sort": "key"}` for spending month by month.
3. `tests/modules/expenses/test_query_compiler.py` is the property test the plan asks for: 500
   random queries checked for their shape, and 200 run on MongoDB against a plain Python
   reading of random entries, with a fixed seed.

### Other currencies

1. `POST /v1/expenses/entries` with `"currency": "USD"`, `"amount_minor": 12000` and a date.
   The entry keeps `currency` USD and what it was in your base currency that day:
   `base_currency`, `fx_rate` and `amount_base_minor`. Rates come from Frankfurter (free, no key,
   `FX_PROVIDER_URL`) and are kept in `fx_rates`, so each currency and day is asked once.
2. `GET /v1/expenses/entries/summary` and every other total count `amount_base_minor`, so they
   stay the same when rates move. `GET /v1/fx/rates?base=USD&date=2026-09-05` shows the rates
   used.
3. Editing an entry keeps its currency (a different one is 422 `EXPENSE_CURRENCY_UNSUPPORTED`).
   A currency with no rate is 422 `FX_CURRENCY_UNSUPPORTED`; with the provider away and nothing
   kept, saving answers 503 `FX_UNAVAILABLE` and saves nothing. Files over
`limits.max_bytes` or `limits.max_rows` answer 422 `EXPENSE_IMPORT_TOO_LARGE`; the route has its
own rate limit (`RATE_LIMIT_IMPORTS`).

## Admin console and account security: try it

Phase 10 (ADR-0017). Sign in as the bootstrap admin (`BOOTSTRAP_ADMIN_EMAIL`, `poe seed`) for
the admin routes. Everything under `/v1/admin` answers 403 to anyone else.

### Accounts

1. `GET /v1/admin/users?q=asha&state=active` finds accounts. `GET /v1/admin/users/{id}` adds
   how many devices are signed in.
2. `PATCH /v1/admin/users/{id}` with `{"role": "admin"}` or `{"disabled": true}`. Disabling
   signs the account out everywhere.
3. `POST /v1/admin/users/{id}/revoke-sessions` signs the account out everywhere.
4. `POST …/reset-password` returns a one-time password, once. The user must change it at their
   next sign-in, and two-step sign-in is turned off so they can get back in.
5. Changing your own account here is refused with 409 `ADMIN_SELF_CHANGE`. A change that would
   leave no active admin is undone with 409 `ADMIN_LAST_ADMIN`.
6. `GET /v1/admin/expenses/entries?user_id=…` lists someone's entries. `PATCH
   /v1/admin/expenses/entries/{id}` with `{"revision_no": 3, "reason": "…", "amount_minor": 1200}`
   corrects one: a revision by you, plus an `entry.admin_corrected` audit entry.

### Allowlist and modules

1. `POST /v1/admin/allowed-emails` with `{"email": "…", "note": "…"}` lets that address
   register while `AUTH_REGISTRATION_MODE=allowlist`.
2. `GET` lists entries from both sources. Entries from `AUTH_ALLOWED_EMAILS` can only be
   removed from the env.
3. `PATCH /v1/admin/modules/{key}` with `{"status": "beta"}` or `{"global_enabled": false}`
   changes a module for everyone. `null` puts the manifest's value back.

### Usage and audit

1. `GET /v1/admin/analytics/usage?from=…&to=…` returns counts only: sign-ups per day, active
   users, assistant answers by AI or by rules (and the fallback rate), AI calls by provider,
   and errors by code.
2. `GET /v1/admin/analytics/db` returns the database size per collection, against
   `ADMIN_DB_QUOTA_MB`.
3. `GET /v1/admin/audit-logs?owner_id=…` returns who changed what: roles, disabling, resets,
   allowlist, modules, two-step sign-in, exports and deletions. Secret-looking details are
   stored as `[redacted]`. Entries expire after `AUDIT_RETENTION_DAYS`.

### Two-step sign-in

1. Set `AUTH_FIELD_ENCRYPTION_KEY`. `.env.example` shows how to generate one. Without it, setup
   answers 503 `AUTH_TWO_FACTOR_UNAVAILABLE`.
2. `POST /v1/auth/totp/setup` returns the secret and an `otpauth://` URI for an authenticator
   app.
3. `POST /v1/auth/totp/enable` with a code from the app and your `password` (or a fresh
   `google_id_token`, for an account without one) turns it on. The response holds the recovery
   codes, shown this once. Turning it off takes the same.
4. `POST /v1/auth/login` now answers `{"two_factor_required": true, "challenge_token": …}`.
   Send that token and a code (or a recovery code) to `POST /v1/auth/login/totp`. Each code
   works once.

### Your data

1. `GET /v1/me/export` downloads everything kept about you as one JSON file. Secrets such as
   password and token hashes are left out.
2. `DELETE /v1/me` needs these fields:
   - `confirm_email`;
   - `password`, or `google_id_token` for an account without a password;
   - `code`, if two-step sign-in is on.

   It deletes every record you own, then the account.
3. `docs/privacy-policy-template.md` is a policy to fill in, including the Play Store Data
   Safety answers.

## Reminders on a device (Web Push): try it

Phase 11 (ADR-0018).

1. `.venv\Scripts\poe vapid-keys` prints `PUSH_VAPID_PUBLIC_KEY` and `PUSH_VAPID_PRIVATE_KEY`.
   Put them in `.env` with `PUSH_SUBJECT=mailto:you@example.com`. Keep the private key secret.
2. In the app (a production build: `npm run build` then `npx vite preview` in the frontend), open
   Notifications → "Reminders on this device" → Turn on, and allow notifications.
3. "Send a test" pushes to every browser you turned on (`POST /v1/push/test`).
4. A due reminder is pushed when the jobs run (the cron's `POST /v1/internal/jobs/all/run`, or
   your own bell), by the `reminders_push` job. It pops up even with the app in front, and the
   bell updates too.
5. Sign out: that browser's pushes stop (so do a password change's other devices, and new VAPID
   keys for every browser).

Push needs HTTPS (or localhost). On iPhone and iPad it works only for the app added to the Home
Screen. Push services are reached over the internet: a network that intercepts TLS stops them,
while in-app reminders keep working.

## Tasks

| Command | What it does |
|---|---|
| `poe dev` | Run the API with reload |
| `poe test` | pytest with coverage (gate: 80%). Needs MongoDB; uses a throwaway database |
| `poe lint` | ruff check, format check, mypy `--strict` (app, tests and each skill's scripts), import-linter |
| `poe fmt` | Auto-format and auto-fix |
| `poe audit` | pip-audit on the pinned runtime dependencies |
| `poe actionlint` | Lint every GitHub Actions workflow |
| `poe check` | **Everything CI runs**: lint, test, audit, actionlint |
| `poe lock` | Re-pin `requirements/*.lock` from `pyproject.toml` |
| `poe sync` | Make `.venv` match `requirements/dev.lock` exactly |
| `poe seed` | Create or promote the bootstrap admin (idempotent) |
| `poe reset-password --email …` | Admin password reset: prints a one-time password, forces a change, turns 2FA off (audited) |
| `poe vapid-keys` | Print a new VAPID key pair for Web Push (`PUSH_VAPID_*`) |
| `poe new-module --spec <file>` | Make a working module in both repositories from a spec (`docs/modules/new-module.md`) |
| `poe backup` | Back up the whole database (`MONGO_URI`) to a new folder under `BACKUP_DIR`, encrypted with `BACKUP_ENCRYPTION_KEY` (required for a database on another machine) |
| `poe restore --from backups/<folder>` | Restore a checked backup into empty collections (`--drop` replaces them); asks for the target `host/database` |
| `poe smoke-live --app https://… [--api https://…]` | Check the live app end to end from outside (`SMOKE_EMAIL`/`SMOKE_PASSWORD` also sign in) |
| `poe openapi` | Write `openapi.json` for the frontend type generator |
| `poe eval-parser` | Accuracy of the expenses rule parser by language and field (`--misses` lists errors) |
| `poe docker-build` | Build the production image locally |

Prefix each command with `.venv\Scripts\` if the venv is not activated.

**Tests and MongoDB.** By default the tests use `mongodb://localhost:27017`. Set
`TEST_MONGO_URI` in `.env` or in your shell to use another server. Each run creates a
database named `tt-test-<random>` and drops it afterwards. Settings variables from your
shell or `.env` are ignored during tests, so results are reproducible.

## Dependencies

- Add or change a dependency in `pyproject.toml`, then run `poe lock` and `poe sync`.
- Both lock files are committed: `requirements/base.lock` (runtime, used by Docker) and
  `requirements/dev.lock` (development and CI).

## Docker (production parity)

```powershell
docker compose up --build   # API on :8000, MongoDB 8.0 on host port 27018
```

The image is multi-stage, runs as a non-root user, has a health check, and logs JSON. Render
injects `PORT`.

## A new module

1. Copy [`docs/modules/example-module.yaml`](docs/modules/example-module.yaml) and fill in the
   module's key, icon, colour, and texts in English, Tamil and Hindi.
2. Run `.venv\Scripts\poe new-module --spec <file>` (Node 24 on `PATH`). It:
   - writes the backend module and its tests here, and the frontend module next door;
   - updates the frontend's API types;
   - runs the new module's tests on both sides.
3. Grow it from there.

[`docs/modules/new-module.md`](docs/modules/new-module.md) says what it makes.
[`docs/roadmap.md`](docs/roadmap.md) lists the upcoming modules and how to build one.
[`docs/mobile.md`](docs/mobile.md) covers the paths to the Play Store and iPhone.

## Deployment

[`docs/deployment.md`](docs/deployment.md) is the runbook: the accounts to create (Atlas,
Render, Cloudflare; all free, no card), the secrets to make, the first deploy from
[`render.yaml`](render.yaml), the first admin, the GitHub secrets, the live checks, and how to
run it day to day (backups, monitoring, limits, rotating secrets). In production:
- the API answers only through the app's `/api` proxy (`SECURITY_PROXY_SECRET`);
- it refuses to start with an unsafe setting;
- it gzips answers over 1 KB.

See ADR-0019.

## Project layout

```
app/
  main.py            create_app() factory: logging, DB, middleware, error handlers, routers
  core/              plumbing with no knowledge of users or modules
    config.py        typed settings groups (the only place that reads the environment)
    logging.py       structlog setup + redaction
    errors.py        AppError hierarchy + {code,message,details,request_id} envelope
    middleware.py    request id + access log, security headers, CORS
    db.py            PyMongo async client, lazy Beanie init, readiness
    deps.py          FastAPI dependencies (settings, database, clock) from app.state
    documents.py     base documents (timestamps; owned = user_id + module_key + soft delete)
    repository.py    ScopedRepository: every query filtered to the caller's Scope
    pagination.py    page/page_size/sort parameters and the Page[T] response
    revisions.py     append-only history without transactions; create once per client key
    idempotency.py   the Idempotency-Key request header
    money.py         minor units <-> decimal amounts (ISO-4217 exponents)
    daterange.py     DateRange: calendar days in the user's time zone, previous period
    concurrency.py   settle(): optional work with a timeout that never breaks the caller
    security.py      Argon2id hashing, password policy, JWT, token digests
    rate_limit.py    per-client rate limits (limits library)
  platform/          shared capabilities used by every module
    auth/            register/login/refresh/logout, sessions, Google, lockout, operator tasks
    users/           User model, /v1/me
    health/          /healthz, /readyz
    modules/         module manifest, discovery, registry, /v1/modules, availability gate,
                     tools.py + skills/manage-modules (agent tools and skill)
    agents/          skill-driven agent runtime: catalog, tools, policy, providers, /v1/agents
    assistant/       agents/assistant.md: the agent behind "Ask" in the command palette
    dashboard/       widget contract (@widget), /v1/dashboard
    jobs/            job contract (@job), daily leases, catch-up, /v1/internal/jobs (cron)
    activity/        ActivityLog, /v1/activity, activity widgets
    notifications/   Inbox, /v1/notifications, tools.py + skills/check-notifications
    reminders/       Reminders (schedule, cancel, deliver once), /v1/reminders (snooze, dismiss)
    search/          search contract (@search), /v1/search across the modules in use
    fx/              exchange rates (Frankfurter), cached per currency and day, /v1/fx/rates
    admin/           /v1/admin/*: the only place an unrestricted Scope is allowed
    context.py       ActorContext: what tools and widgets receive
    plugins.py       folder discovery shared by modules, agents, skills, tools, widgets and jobs
  modules/           feature modules, auto-discovered
    expenses/        entries with history, categories, products and prices, analytics and
                     insights, budgets, goals, repeating entries and bills, loans, tile,
                     widgets, tools, jobs, skills track-expenses, tidy-products
                     and explain-spending (beta);
                     parsing/: the rule parser (vocabulary in skills/track-expenses/references)
    medicine/ ...    the other seven: manifest only, coming soon
tests/               pytest suite (real MongoDB)
scripts/             seed.py, reset_password.py, export_openapi.py, hooks/venv-run.sh
docs/                plan, ADRs, architecture, reference analysis, free-tier limits
```

## Conventions (enforced by tooling)

- **Errors:** raise `app.core.errors.AppError` subclasses. `HTTPException` is banned by
  ruff. Every error response has the envelope shape.
- **Configuration:** only `app/core/config.py` reads the environment. `os.environ` and
  `os.getenv` are banned elsewhere.
- **Layers:** `app.core` must not import `app.platform`, and platform must not import
  modules. import-linter enforces this in CI.
- **Data access:** user-owned records are read and written only through `ScopedRepository`.
  Another user's record answers 404, never 403. `Scope.unrestricted()` is allowed only under
  `app/platform/admin/`, and a test enforces this.
- **Auth by default:** every `/v1` route needs a bearer token unless it is on the short public
  list in `tests/architecture/test_access_rules.py`. The isolation matrix
  (`tests/isolation/`) must cover every route that takes an id.
- **Logs:** values whose key contains `password`, `token`, `cookie`, `email` and similar are
  masked automatically (`LOG_REDACT_KEYS`).
- **Commits:** never commit `.env`. The pre-commit hook blocks it, and gitleaks scans in CI.
- **Line endings:** files are stored as LF in git (`.gitattributes`).

## CI

`.github/workflows/ci.yml` runs on every push and pull request to `develop` and `main`:
- the quality job (lint, test against a `mongo:8.0` service, pip-audit, actionlint);
- a gitleaks secret scan;
- a Docker image build.
