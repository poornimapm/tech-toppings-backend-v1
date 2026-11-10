# Travel

*Plan your trips, day by day.* A person's trips, each with its travellers, a day-by-day plan of
activities, a checklist and packing list, bookings and a budget. The plan changes one scoped step at
a time, and the trip's history says what each change touched and can take it back. Beta since
Phase 18 (ADR-0025; the owner's plan: `modules-usecases/Travel_Companion_Plan.md`), for everyone.
Nothing in it uses AI: a trip's mode is only stored.

## What exists now

The module's pages (`src/modules/travel`), in English, Tamil and Hindi:

- **Home**: a greeting; *Plan a trip*, *All my trips*, and *Prepare tomorrow* and *Packing* for the
  next trip (the one under way, else the soonest); the trips under way and coming up, each with its
  countdown, readiness (or *No tasks yet*) and next step with its reason.
- **My trips**: all of them, or those coming up, under way or past (kept in the address).
- **Plan a trip**, in five steps: where and when; who (each traveller with their own passport
  country, and more only if wanted); style, budget and the reminder; the mode (Static; Basic AI,
  which works as Static; Premium, locked); a check to edit before saving.
- **A trip**, with tabs:
  - *Overview*: the countdown, readiness, the next step with *Do it now*, what is next and still
    open, the trip's details (edit it, or delete it with a moment to bring it back), its mode.
  - *Itinerary*: a day strip and a timeline. Add, edit (what it changes shown first), move up or
    down (saved at once, with Undo), mark done, skip, replace or reschedule (a preview, then *What
    changed?*), remove.
  - *Prepare*: *Get me ready*, *Prepare tomorrow*, or any day of the trip (those still to do
    marked): its plan, bookings, getting around, its checklist and the whole trip's, and
    suggestions for the day.
  - *Packing*: by bag, with progress and quantities, and suggestions shown before they are added.
  - *Budget*: estimated against spent by category, what is left, who owes whom, and costs with no
    rate listed apart; costs added, changed and removed; the trip's budget.
  - *Bookings*: as entered, with a status badge; a form; linked to activities, or unlinked.
  - *History*: every change, newest first, with Undo; a refused undo says why.
- The Undo offered after a change takes back the event that change answered (`event_id`). A
  checklist or packing tick made with no network waits on the phone (the outbox, `travel.tick`)
  and is sent when back.

Outside its pages:

- **Welcome tile**: how many trips are still to come or under way.
- **Dashboard**: the next trip (`travel_next_trip`): the one under way, else the soonest ahead.
- **Command palette**: trips by title.
- **Activity feed**: a trip planned, edited, deleted or brought back, and a booking or expense
  added. A mode switch is not noted.
- **Notification**: `travel.prepare_due`, the day before a trip that asked for it
  (`remind_prepare`). It opens the trip's first day to prepare.

## API (`/v1/travel`, the signed-in person's own)

| Route | What it does |
|---|---|
| `GET /options` | What the forms offer and check (`data/travel.yaml`), and whether Premium can be saved |
| `POST`, `GET /trips` | Start a trip (`Idempotency-Key`); list them (`phase` = `upcoming`, `active`, `past`) |
| `GET`, `PATCH`, `DELETE /trips/{id}` | One trip; change it (only what is sent, with `revision_no`); delete it (soft) |
| `POST /trips/{id}/undelete` | Bring a deleted trip back, with everything in it, while the person has room for it |
| `GET /trips/{id}/overview` | Countdown, the next activities, what is open, `readiness`, `next_action` |
| `GET`, `POST /trips/{id}/activities` | The plan in timeline order (`day`); add an activity, last in its day |
| `POST /trips/{id}/activities/reorder` | A new order for one day: every activity of that day, each once |
| `GET`, `PATCH`, `DELETE /activities/{id}` | One activity; edit it (not its day); remove it (answers `{event_id}`) |
| `POST /activities/{id}/change` | `done`, `skip`, `replace` or `reschedule`: a preview until `apply` is true |
| `GET`, `POST /trips/{id}/checklist` | Checklist and packing items (`day`, `bag`, `activity_id`); add one |
| `POST /trips/{id}/checklist/suggest` | Suggestions by rule (`day`, `packing`): a preview until `apply` is true |
| `PATCH`, `DELETE /checklist/{id}` | Change or tick an item; remove it (a removed suggestion isn't suggested again; answers `{event_id}`) |
| `GET /trips/{id}/prepare?when=` | One day's plan: `today`, `tomorrow` or a date. Activities, checklist, bookings, transport, and `open_days` |
| `GET`, `POST /trips/{id}/bookings` | Bookings, those with no time first; add one as the person has it |
| `GET`, `PATCH`, `DELETE /bookings/{id}` | One booking; edit it; remove it (only while no activity links to it; answers `{event_id}`) |
| `GET`, `POST /trips/{id}/expenses` | Estimates and actual costs, by day; add one |
| `PATCH`, `DELETE /expenses/{id}` | Edit an expense; remove it (answers `{event_id}`) |
| `GET /trips/{id}/budget` | Estimated against actual by category in the trip's currency, unconverted amounts, who owes whom |
| `GET /trips/{id}/events` | The trip's history, newest first |
| `POST /events/{id}/undo` | Take one change back, if nothing it touched has changed since |

Every edit carries the `revision_no` it was made from, and a record that has moved on answers 409
`VERSION_CONFLICT` (a checklist item's edit may leave it out, so a tick made offline can be sent
later).
Another person's trip, or anything in it, answers 404, as an unknown id does. A create sent again
with the same `Idempotency-Key` answers what the first one made, before any other check (a cap it
filled, say).

Every write answers `event_id`: the event of the trip's history it recorded, so the app undoes
exactly that one. It is `null` when nothing was recorded: an edit that changes nothing, a tick (only
the item's own revisions keep it), a mode switch, or bringing back a trip that wasn't deleted. A
create sent again answers the first one's. A trip, an activity, an item, a booking or an expense
written answers the record with `event_id` beside its fields; a reorder answers the day with the
reorder's `event_id` on each activity; removing an activity, an item, a booking or an expense
answers only `{"event_id": ...}` (200). Deleting a trip still answers 204: its event can't be
undone, and the trip comes back by `undelete`. A change or suggestions applied answer `event_id`
too (`null` in a preview).

Refusals: `TRAVEL_PREMIUM_LOCKED` (403), `TRAVEL_TRIP_DATES_INVALID`, `TRAVEL_TRAVELLER_UNKNOWN`,
`TRAVEL_ACTIVITY_UNKNOWN`, `TRAVEL_BOOKING_UNKNOWN`, `TRAVEL_DAY_OUTSIDE_TRIP`,
`TRAVEL_CHECKLIST_DAY_MISMATCH`, `TRAVEL_REORDER_MISMATCH` (422); `TRAVEL_TRIPS_FULL`,
`TRAVEL_ACTIVITIES_FULL`, `TRAVEL_CHECKLIST_FULL`, `TRAVEL_BOOKINGS_FULL`, `TRAVEL_EXPENSES_FULL`,
`TRAVEL_ACTIVITIES_OUTSIDE_DATES`, `TRAVEL_CHECKLIST_OUTSIDE_DATES`, `TRAVEL_TRAVELLER_IN_USE`,
`TRAVEL_BOOKING_IN_USE`, `TRAVEL_ACTIVITY_STATE`, `TRAVEL_SUGGESTION_TAKEN`,
`TRAVEL_UNDO_UNAVAILABLE`, `TRAVEL_UNDO_DONE`, `TRAVEL_UNDO_STALE` (409); and the usual
`VALIDATION_ERROR` and `VERSION_CONFLICT`.

## How it works

- **Data** (MongoDB): `travel_trips`, `travel_activities`, `travel_checklist`, `travel_bookings`
  and `travel_expenses`, each with its `..._revisions`, and `travel_events`, the trip's history.
  Every record but an event keeps every revision and is soft-deleted. Deleting a trip leaves
  everything in it as it is, out of reach until the trip is brought back.
- **Dates and times** are plain: days `YYYY-MM-DD`, times `HH:MM`, a booking's `starts_at` and
  `ends_at` `YYYY-MM-DDTHH:MM`, all local where it happens. A trip's "today" is at its first
  destination's time zone, else the person's own.
- **Money** is in minor units. A trip has one currency (the person's base currency unless they
  choose another), and an expense keeps the currency it was in.
- **Travellers** each have their own passport country, and nothing about them is inferred. Ids in
  a body (an activity's `booking_id`, an item's or an expense's `activity_id`, `traveller_id`,
  `paid_by`, `shared_with`, a reorder's `activity_ids`) must be of the same trip. A traveller
  stays while a checklist item or an expense names them.
- **Mode** (`static`, `basic`, `premium`) is one field of the trip: switching it writes nothing
  else, not even an event or a feed entry (the trip's own revision keeps it). Basic can be saved,
  and does what Static does. Premium can't be saved (403 `TRAVEL_PREMIUM_LOCKED`, through
  `premium_allowed`, false for everyone).
- **Changes and history** (`events.py`): a change is planned as steps, one write to one record
  each, and the event keeps the steps that take it back. Skipping or replacing an activity sets its
  own untouched, unfinished checklist items to not applicable; items the person edited or
  completed are kept and listed under `kept`. The booking it links to (on a skip, a replacement or
  a reschedule) and its estimates (on a skip or a replacement), and its own estimated cost, are
  listed under `needs_review` and never changed. A checklist item in `kept` or `what_changed`
  that is still the suggestion as its rule words it (a tick keeps it so) carries its
  `suggestion_key`, so the app words it in the reader's language. Rescheduling moves the activity
  and its items' day and reports both days. Nothing on any other day changes. An undo reverses one
  event while nothing it touched has changed since, and is an event itself. Each step is checked,
  and what takes it back kept, before it is written, so a write that lands and then fails (the
  read after it, say) is taken back too. A step that fails, for any reason, has itself and those
  before it taken back, each on its own (a field someone has written since keeps their value);
  anything that can't be taken back is recorded as an event all the same, so the history shows it.
  What follows a write (a trip's reminder) runs only once its change is recorded. The caps on
  trips, activities, items, bookings and expenses hold on every write that makes or brings back a
  record, an undo's and a trip's undelete too.
- **Suggestions** (`suggestions.py`) come from rules in `data/travel.yaml`: by kind of activity,
  and a packing list by the trip's length, for one person. They are stored as suggestions
  (`source` rule) and never overwrite the person's own work: asking again adds only what isn't
  there, and a removed suggestion stays removed.
- **Bookings** are only ever what the person entered. They save `entered` or `confirmed` (they
  checked it with whoever it is with); `extracted` and `verified` are kept for the app's own
  reading and checking, which nothing does yet. A day's plan lists the bookings that start or end
  on it and those its activities link to, unless skipped or replaced.
- **Readiness** is the trip's completed items over those that still apply (not applicable ones
  left out). With none that apply, it is `null`: no tasks yet, and no figure.
- **Next action** is the first rule that applies, in the order `overview.next_actions` lists them
  (`overview.py` says when each does): the trip has ended, add activities, prepare today, prepare
  tomorrow, confirm bookings, finish packing, start packing, finish the checklist, add tasks (a
  trip with nothing to do or pack is never called ready), all set. It carries a `reason` and the
  figures for it. Prepare today, prepare tomorrow and finish the checklist also carry the `day` to
  open: for the checklist, the first day holding an item still to do (`null` when only the whole
  trip's are). A day's plan lists `open_days`, every day of the trip still holding an item to do,
  packing ones too.
- **Budget** (`budget.py`): each expense is converted to the trip's currency through the platform's
  rates (`Fx`, Frankfurter) on its own day, or today's when it has none or its day is still to
  come. Only a rate of that very day counts: one whose rate can't be had (an older day's, which
  `Fx` falls back to when its provider is down, included) is listed under `unconverted`
  (`unavailable` or `unsupported`) and counted nowhere. Once the provider fails, budgets use only
  the rates already kept for `budget.fx_rest_seconds` before asking it again. A failure is `Fx`
  raising `FxUnavailableError`, answering an older day's rate, or answering today's as kept from
  before its cache's time (`FX_CACHE_TTL_HOURS`: it asked again and couldn't); that last still
  counts, as today's own.
  A cost whose rate can't be had just now is still `unsupported` when the provider doesn't have
  its currency or the trip's: it said so in this process, or none of the rates kept (each answer
  names every currency it has) names it. This holds for the lookup that finds the provider failing
  as for those while budgets rest, so the reason never turns on which came first. Who owes
  whom counts only actual costs with a payer: each is split evenly among those who share it (the
  payer alone when nobody does), and the minor units an even split leaves over go one each to the
  first of them in the trip's order of travellers. The largest debt is then paid to the largest
  credit until all are even, ties by that order: the same records always give the same transfers.
- **Reminders**: with `remind_prepare`, one reminder (`travel.prepare_due`) the day before the trip
  starts, through the platform's reminders. It moves with the dates and goes with the setting or
  the trip, an undo's write too, once the change is recorded.
- **Configuration** is data: `app/modules/travel/data/travel.yaml` holds:
  - the longest trip, and the most trips, destinations, travellers and interests;
  - the kinds of activity, those that are themselves transport, the most activities and the
    longest one;
  - the most checklist items and the largest quantity;
  - the most bookings, the budget's categories and the most expenses, and how long budgets leave
    the rates provider be once it fails;
  - how many next activities the overview shows, and the order of the next actions;
  - the suggestions, by kind of activity and for packing, and the most added at once.
- **Privacy**: everything is the person's own (anyone else gets 404), in their export, and goes
  with their account.
- **Languages**: the API sends keys (event kinds, reasons, next actions, categories, suggestion
  keys, feed and widget codes) for the app to word in English, Tamil and Hindi.

## Limits

- **No AI**: suggestions are rules, and a trip's mode is only stored.
- **No weather, no entry or visa rules, no looked-up bookings or schedules**: there is no reliable
  free source for them. Nothing of the kind is ever made up.
- **One time zone per trip**: a trip's today is at its first destination's time zone, else the
  person's own, so a trip across several time zones is only as precise as that.
- **No transactions** (ADR-0002, ADR-0025): a change is written step by step, and a step that
  fails partway has itself and those before it taken back, but:
  - the process stopping midway could leave part of a change with no event;
  - a step whose write failed and whose taking back failed too (the database gone) is recorded as
    written, as it may have been;
  - an update that lands just as another request removes the record is taken as refused, so it
    isn't taken back;
  - a record made outside a change (an add; a trip made, deleted or brought back) and its event
    are two writes: if the event's fails, the record is left with none;
  - the part of a change recorded because it couldn't be taken back has the whole change's
    headline in the history.
- **A reminder that can't be moved**: the trip's change is saved and in the history (and can be
  undone), but the answer is an error and the reminder stays as it was until the trip's next write.
- **Caps and races**: two creates sent at the same moment can both pass a cap's count, so a trip
  could hold one record past it.
- **Only the latest change to a record can be undone**: an undo checks whole records, so a later
  edit to any field of one blocks it, and an undo moves the record on too. An undo, and a trip
  made, deleted or brought back, can't be undone (a deleted trip comes back by `undelete`).
- **Ticks made offline are last-write-wins**: a tick carries no revision, so of two devices
  ticking one item, the last one sent wins. Only ticks wait offline; every other change needs the
  network.
- **An expense with no day, or a day still to come,** is converted at today's rate, so its figure
  moves with the rates until its day.
- **Estimated costs on activities** stay on the activity: the budget counts expenses only, so
  nothing is counted twice.
- **The rates provider's rest** is this server process's own memory. A currency it is found not to
  have stays so until the process restarts; only the reason given for an unconverted cost depends
  on it.
- **Screens**:
  - Prepare's day picker loses its *still to do* marks for a moment while another day loads.
  - A booking's *Unlink* that fails goes unseen if Bookings is left before it answers.
  - Saving the edit of a trip that has already started turns its reminder off (none can come
    then), and the history shows that as a change.
  - The history doesn't show the figures of a cost's new amount: a change line doesn't carry the
    cost's currency.
  - A browser without `Intl.supportedValuesOf` offers no time zones and only the base currency.
  - The Tamil and Hindi words haven't been reviewed by a native speaker. Tamil at phone width was
    checked only in the e2e test, with the API stubbed, and Hindi not in a browser.
- **Not measured against Atlas**: one suggestions request makes about four round trips per item
  (about 400 for 100 items), an overview about seven, and Home asks for one overview per trip it
  shows (every trip under way and up to five coming up).
