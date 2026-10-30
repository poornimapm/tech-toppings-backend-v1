# Labor Management

*Workers, days and wages, simply.* An owner's projects (a farm, house help, an office), their
workers with photos, attendance by day, wages and payments, reports, and every change kept.
Beta since Phase 16 (ADR-0023). Only the accounts the admin gives it to see it; the admin sees
every owner's.

## For the admin

1. Admin console → **Users** → **Create an account**: email, name, tick *Labor Management*.
   The one-time password is shown once: give it to the owner privately. They choose their own
   at the first sign-in.
2. To give Labor to an existing account, or take it back: open the account → *Modules only
   some accounts may use*. Taking it back hides it; their records stay.
3. In the app, **Labor Management → All owners** shows every owner, their projects and their
   history (read only; ID pictures are never shown there).

## For an owner

- **Overview**: today across open projects, what today costs, what is owed, and *Record today's
  attendance* for each project.
- **Projects**: as many as needed, overlapping freely. Each stays open until closed. Closing
  keeps everything; a closed project takes no new attendance or payments until reopened.
- **Workers**: name, gender (if wages differ by gender), phone, a worker number (given
  automatically), a photo (the phone's front camera), and a picture of their ID only if
  needed (only the owner sees it). A worker who stops is made *not working*, never deleted.
  Each worker's *days and payments* shows their last month: every day marked, what it earned,
  every payment (voided ones marked), and the totals.
- **Attendance**: pick the day (today by default); each worker's card has *Full day*, *Half
  day*, *Absent*. A tap saves. *Mark the workers left: full day* marks the rest at once. The
  day's counts and wages stay in view.
  - **No network?** Keep tapping: each mark waits on the phone (*Waiting to send* on its card)
    and goes when the network is back. A day must have been opened while online. If someone
    marked that worker differently on another phone meanwhile, the newer mark stays and the
    waiting one is shown as not sent, to discard.
  - **Say the marks**: "Ravi full day, Meena half day, the rest absent" (or type it), in
    English, Tamil or Hindi. The marks are shown to check (who, which mark; a name not
    recognised is asked about), and saved only with *Save*. The assistant does the same from
    anywhere when there is one open project. Reading needs the network.
- **Wages**: the project's usual daily wage (one for all, or for women and men), and each
  worker's own, changed from a chosen day (today or before) with a reason. Days already
  recorded keep their wage; a day recorded later is paid at the wage of that day.
- **Payments**: what each worker earned (from their days) and was paid (money given) up to
  today, kept apart, and what is owed. Record a payment (cash, UPI, bank); a mistaken one is
  voided with a reason and stays in the history.
- **Reports**: between two days, by day, week or month and by worker. *Print or save as PDF*
  prints the page (the browser makes the PDF, Tamil and Hindi shaped right); *Download CSV*
  saves the figures, then each day marked and each payment, for a spreadsheet.
- **History**: every change, who made it and when, each field from and to.

## API (`/v1/labor`, the owner's own)

| Route | What it does |
|---|---|
| `GET /options` | What the forms offer and check (`data/labor.yaml`) |
| `GET /overview` | Today across the open projects, what is owed, the latest changes |
| `GET /audit` | The owner's audit trail; `project_id`, `entity`, `action`, `actor`, `from`, `to` |
| `POST`, `GET /projects` | Add a project; list them (`state`, `q`) |
| `GET`, `PATCH /projects/{id}` | One project; change it (renaming keeps everything) |
| `POST /projects/{id}/close`, `/reopen` | With an optional note |
| `PUT /projects/{id}/rates` | The usual daily wages for new workers |
| `POST`, `GET /projects/{id}/workers` | Register a worker; list them (`active`, `q`) |
| `GET`, `PUT /projects/{id}/attendance?day=` | A day's sheet; mark workers full, half or absent (a mark kept offline sends `expected`, the mark it saw) |
| `POST /projects/{id}/attendance/read?day=` | Marks said or typed, read into a draft to check (the `mark-attendance` skill); nothing is saved |
| `POST`, `GET /projects/{id}/payments` | Record a payment (`Idempotency-Key`); list them |
| `GET /projects/{id}/report?from&to&group` | Earned, paid and owed, by `day`, `week` or `month`; each worker by name with what they earned, were paid and are owed to date |
| `GET /projects/{id}/report.csv?from&to&group` | The same as a CSV file, then each day marked and each payment |
| `GET`, `PATCH /workers/{id}` | One worker; change their details |
| `PUT /workers/{id}/wage`, `GET /workers/{id}/wages` | A new wage from a day on; its history |
| `GET /workers/{id}/history?from&to` | Their days and payments between two days, newest first, and the totals |
| `PUT /workers/{id}/active` | Working, or not |
| `PUT`, `GET`, `DELETE /workers/{id}/photo`, `/id-card` | The picture is the request body (JPEG, PNG or WebP) |
| `POST /payments/{id}/void` | Void a payment, with the reason |

The admin's, read only, under `/v1/admin/labor`: `owners`, `projects` (`owner`, `state`, `q`),
`projects/{id}/workers`, `projects/{id}/report`, `projects/{id}/report.csv`, `audit` (`owner`
and the owner's filters). Audit entries carry `labels`: the names of the workers a change holds
by id.

Refusals: `LABOR_PROJECT_CLOSED` (409), `LABOR_WORKER_CODE_TAKEN` (409), `LABOR_WORKER_UNKNOWN`
(422: not in this project, or not working, for a new mark), `LABOR_DAY_IN_FUTURE` (422),
`LABOR_MARK_CHANGED` (409: a mark kept offline whose day was marked otherwise since),
`LABOR_WAGE_DATE_INVALID` (422: a new wage starting after today),
`LABOR_PAYMENT_VOIDED` (409), `LABOR_PERIOD_INVALID` (422), `LABOR_EXPORT_TOO_LARGE` (422),
`FILE_TOO_LARGE` (413), `FILE_TYPE_UNSUPPORTED` (415), and `MODULE_NOT_FOUND` (404) for an
account not given Labor.

## How it works

- **Data** (MongoDB): `labor_projects`, `labor_workers`, `labor_wage_changes`,
  `labor_attendance` (unique per owner, project, worker and day), `labor_payments` (unique per
  owner and idempotency key), `labor_audit`, and the pictures in the platform's `stored_files`.
- **Money** is in minor units (paise) of the owner's currency. A half day rounds half up to
  whole paise.
- **History**: an attendance mark keeps the wage it was first recorded at, so a wage change
  never rewrites past earnings or balances. A new mark takes the wage in effect on its day:
  of the changes that started by then, the one made last. Payments are voided, never deleted. Every change
  is in `labor_audit` after it is saved, and nothing changes an entry.
- **Configuration** is data: `app/modules/labor/data/labor.yaml` holds:
  - project kinds and the most projects;
  - worker numbers, genders, and the most workers;
  - picture types, limits and sizes;
  - the largest daily wage and the units of each mark;
  - payment methods and the largest payment;
  - the longest report, and the most rows in a report file;
  - the most characters of marks read at once.
- **Offline** (ADR-0010, ADR-0024): a tap with no network waits in the app's outbox
  (`labor.marks`) with the mark it saw, and is sent when back; an old tap never overwrites a
  newer mark.
- **Marks by voice** (ADR-0024): the `mark-attendance` skill (`skills/mark-attendance`: its
  words in `references/attendance.yaml`, the rules in `marks_reader.py`) reads them into a draft
  through the `labor_day` and `labor_attendance_draft` tools; it never saves.
- **Languages**: English, Tamil and Hindi. Kannada needs `kn.json` beside each `en.json`
  (`src/modules/labor/i18n`, `strings/`), `kn` in the app's languages, and Kannada words in
  `attendance.yaml` for marking by voice.
- **Not yet**: marking offline after the app is closed and reopened with no network (the
  session doesn't survive a restart: ADR-0003).
