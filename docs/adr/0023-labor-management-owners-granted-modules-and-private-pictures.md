# ADR-0023: Labor Management: owners, granted modules and private pictures

- **Status:** Accepted
- **Date:** 2026-10-30
- **Deciders:** project owner (Phase 16)

## Context

The owner pasted a full specification for a Labor Management module on 2026-10-29: owners (made by
the platform admin) run projects, register workers with a photo and an ID picture, record
attendance by day (full, half, absent), set wages (one for all, or by gender, and each worker's
own, with history), record payments, see daily, weekly and monthly reports, and every change is
in an append-only audit trail with its before and after. The admin oversees every owner. The UI
must suit someone with little reading or digital literacy, on a phone. It is not a coffee-farm
app: project kinds are the owner's.

The owner chose (2026-10-29):
- worker photos and ID pictures in **MongoDB, small and private**, shrunk on the phone and
  served only through owner-checked routes;
- **admin-created owner accounts** (email, name, a one-time password changed at the first
  sign-in); only those accounts see Labor, and the admin sees all;
- **English, Tamil and Hindi now, ready for Kannada**.

## Decision

### Modules only some accounts are given (platform)

- A manifest says who may use its module: `audience="everyone"` (as before) or `"granted"`.
- A grant is kept with the account's own module record (`user_modules.granted_at`,
  `granted_by`), written only by the admin's routes: the account's preferences have no such
  field.
- For anyone not given it, the module isn't there at all: not listed, and its routes answer
  404 `MODULE_NOT_FOUND`. Admins see every module.
- Admin console:
  - `POST /v1/admin/users` creates an account with a one-time password (shown once, hashed as
    every password is) and the modules to give it;
  - `PUT /v1/admin/users/{id}/modules/{key}` gives one or takes it back.
  Both are audited, and the console has *Create an account* and a switch per such module.
- The route matrix gives its accounts every such module, so each route's own owner check is
  what it tests. A module's `isolation.py` may now add the bodies and queries its routes need.

### Private pictures (platform)

- `app/platform/files`: `Files.keep(request, module=, purpose=, types=, max_bytes=)` takes the
  request body as a file of the owner's, its type read from its bytes. `file_response` serves
  it with `Cache-Control: private, no-store` and `nosniff`.
- **One document per file** (`stored_files`, the bytes inside), **not GridFS**: these files are
  a few hundred KB at most, and MongoDB advises single documents below its 16 MB limit. A file
  is scoped, erased and exported like any owned record. The export lists files; their bytes
  stay out of it (`PRIVATE_FIELDS`).
- A replaced or removed picture is deleted for good.
- The phone shrinks pictures first (`shrinkTo`: a face to 480 px, an ID to 1400 px, within the
  server's limits in `data/labor.yaml`).

### The Labor module

- **Records.** Projects; workers (one project each, never shared); wage changes; attendance;
  payments; the audit trail. All belong to one owner (`ScopedRepository`: another owner's id
  answers 404), and none is ever hard-deleted:
  - a project is closed: kept, and refusing new workers, marks or payments until it is reopened;
  - a worker is made inactive;
  - a payment is voided, with a reason.
- **Wages.**
  - A project's usual daily wage, one for all or by gender (the owner's choice), starts a new
    worker's own wage; workers already in keep theirs.
  - A worker's wage changes from a day on (today or before, never later), kept in
    `labor_wage_changes`. The wage on a day is, of the changes started by then, the one made
    last; before every change, the wage the earliest one changed from.
- **Attendance.** One mark per worker, project and day (a unique index): full 1, half 0.5,
  absent 0 (`data/labor.yaml`).
  - The first mark keeps the worker's wage on its day (`rate_minor`), so a day marked late is
    paid at the wage of that day. Changing the mark recomputes from that same rate, so a wage
    change never rewrites the past.
  - Pay is rounded half up to whole paise, in Decimal (`rules.earned`).
  - No day after today.
- **Payments.** Partial or full, any number; one sent twice with one `Idempotency-Key` is kept
  once.
- **Reports.** Earned (from attendance) and paid (payments not voided) are kept apart, by day,
  week (Monday first) or month, by worker, with what is owed to the period's end. The database
  sums each day, and the days are grouped here.
- **Audit.** Each change the services make, after it is saved (a failed one leaves no entry):
  the actor, the server's UTC time, the project, the record, the action, each field from and
  to, a note and the request id.
  - A picture is recorded as changed, never its contents.
  - Read-only routes for the owner (`/v1/labor/audit`) and the admin (`/v1/admin/labor/audit`,
    any owner); none changes an entry.
  - It is the module's own collection, without the platform trail's retention.
- **Routes** each name one record (`/projects/{id}`, `/workers/{id}`, `/payments/{id}/void`,
  the day as `?day=`), so the route matrix covers them.
- **Admin oversight.** `/v1/admin/labor/owners`, `/projects`, `/projects/{id}/workers`,
  `/projects/{id}/report` and `/audit`, read only. ID pictures are never shown there, only
  whether a worker has one.
- **The UI** is a frame with the project picker (kept in the address and remembered on the
  device) and tabs with icons and words: Overview, Projects, Attendance, Workers, Wages,
  Payments, Reports, History (and *All owners* for admins).
  - Attendance is photo cards with three large marks that save on a tap, plus "everyone left:
    full day".
  - States read in words and icons, never colour alone.
  - Kannada needs only a `kn` JSON beside each `en`.

### Module strings load with their language (frontend)

A module's Tamil and Hindi words for the shell (tile, feed, assistant) moved from its manifest
to `strings/{ta,hi}.json`. They load with the chosen language, as the shell's own do, and
English stays in the manifest. This made room in the 200 KB first-load budget for Labor. The
module generator writes the same layout.

## Consequences

- Any later module for some accounts only is one manifest field away.
- Offline attendance is not part of this phase. The app's outbox could carry marks later (it
  does since Phase 17: ADR-0024, with report files and marks by voice).
- Exports could add the pictures themselves as a zip if owners ask; for now they download each
  picture in the app.
- First-load JavaScript is at 198.3 KB of 200: the next module's manifest needs room found.
