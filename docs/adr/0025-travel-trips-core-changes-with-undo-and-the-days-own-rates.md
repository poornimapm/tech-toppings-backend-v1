# ADR-0025: Travel's trips core: changes with undo, the day's own rates and room on the first page

- **Status:** Accepted; amended in part by Phase 19, where Basic turns on the trip's AI help
  (its engine: [ADR-0026](0026-strands-runs-the-agent-loop-background-runs-with-stages.md))
- **Date:** 2026-11-06
- **Deciders:** project owner (Phase 18: "Trips core (no AI needed)", from the plan of 2026-11-05)

## Context

The owner's Travel Companion use case asks for trips planned day by day that change as things
happen, one scoped step at a time, saying what each change touched. The owner's plan
(`modules-usecases/Travel_Companion_Plan.md`, 2026-11-05) splits it into Phases 18 to 22. Phase 18
is the trips core, with no AI; its §6a is the contract each part was built to. The owner also chose,
on 2026-11-05, a locked switch for Premium: no AWS, no payment.

Three things shaped the design:
- MongoDB here has no transactions (ADR-0002), but one change reaches several records (an activity
  and its checklist items, say) and must be undone as one.
- Rates come from the platform's `Fx`, which answers the newest rate it keeps when its provider is
  down. The contract says never to guess a rate.
- First-load JavaScript was 199.3 KB of 200 before Travel, and a module's manifest costs 2 to 3 KB.

## Decision

### A change is previewed, then recorded as one event (rules 3 and 5)

- `POST /v1/travel/activities/{id}/change` (`done`, `skip`, `replace`, `reschedule`) and
  `POST /trips/{id}/checklist/suggest` work the whole change out first. With `apply` false nothing
  is written: the answer is the preview (`what_changed`, `needs_review`, `kept`, `days`). With
  `apply` true the same plan is written and recorded as one `TripEvent`.
- Adding, editing, removing and reordering a trip's records each record one event too. An edit that
  changes nothing records none.
- Only what depends on the activity is reassessed (`changes.EFFECTS`, one effect per kind of
  record). On a skip or a replacement its own untouched, unfinished items stand down; on a move to
  another day all its items move with it. Its booking and estimates are listed under
  `needs_review` and never changed. No other day's record changes.
- An event keeps the steps that take it back. `POST /events/{id}/undo` runs them while every record
  it touched is still at the revision the event left. Whole records count, not fields: an edit to
  any field of one blocks it.
  - Moved on since: 409 `TRAVEL_UNDO_STALE`. Undone already: `TRAVEL_UNDO_DONE`. Nothing to run (an
    undo; a trip made, deleted or brought back): `TRAVEL_UNDO_UNAVAILABLE`.
  - An undo is an event itself, and can't be undone. It moves the records on too, so only the
    latest change to a record can be undone.
  - An undo is stale as well when what it brings back no longer fits: an item naming a traveller who
    has left, or off its activity's day; an expense naming one who has left; an activity's adding
    while items or expenses are tied to it; a booking's adding while an activity links to it; an
    activity linked again to a booking removed since.
- The caps (`data/travel.yaml`) hold on every write that makes a record or brings one back, an
  undo's and a trip's undelete too.

### Written step by step, taken back step by step (ADR-0002)

- A change is planned as steps, one write to one record each. Each kind of record has a store
  (`events.STORES`) that runs a step in this order:
  1. `ready()` checks the step, or refuses it with an `AppError` before anything is written, and
     answers the step that takes it back. That is kept before the write, so a write that lands
     and then fails (the read after it, say) is taken back too.
  2. `write()` does it, and says the revision it left.
  3. `follow()` (a trip's reminder) runs only once the change's event is recorded.
- A step that fails, for any reason, has itself and those before it taken back, the latest first,
  each on its own: one that can't be taken back doesn't stop the rest. A field is set back only
  while it still holds what the step wrote, so what someone wrote since stays theirs. A refused
  write wrote nothing, so nothing of it is taken back.
- What stays recorded when that fails:
  - steps that couldn't be taken back are recorded as an event of the change's own kind, with
    only those records, so History shows them and can take them back later;
  - an undo that fails is put back as the event left it, to be tried again; what of it couldn't be
    put back is recorded as an undo of that part, and the event is then stale;
  - a follow-up that fails leaves the change saved, recorded and undoable. The answer is an error,
    and the reminder keeps its old day until the trip's next write.
- Not covered: the process stopping midway (part of a change, with no event); a step whose write
  and taking back both fail (recorded as written, as it may be); an update that lands while
  another request removes the record (taken as refused, so not taken back); a record made outside
  a change (an add, or a trip made, deleted or brought back) whose event then fails to be written.

### Every write answers the event it recorded

- Each create, edit, reorder and removal answers `event_id`: the event it recorded, or `null` when
  it recorded none (an edit that changes nothing, a tick, a mode switch, bringing back a trip that
  wasn't deleted). The app's Undo takes back exactly that event; it never searches the history.
- A record answers with `event_id` beside its fields (`Travel*Recorded`). A reorder puts its
  `event_id` on every activity of the day, so a move whose own activity wasn't written can still be
  undone.
- Removing an activity, an item, a booking or an expense answers 200 `{"event_id": ...}`, not 204:
  a 204 has no body, and a header would have needed the core's CORS settings changed.
- Deleting a trip still answers 204: its event has nothing to undo, and the trip comes back by
  `POST /trips/{id}/undelete`, which answers its own `event_id`.
- A create sent again with its `Idempotency-Key` answers the first one's record and event, before
  any cap or other check: a retry after the last place went isn't told there is no room.

### A rate of the expense's own day, or none

- The budget converts each expense to the trip's currency through `Fx`, on the expense's own day,
  or today's when it has no day or its day is still to come.
- Only a rate of that very day counts: one from an older day (`Fx`'s answer when its provider is
  down) is refused. The amount is then listed under `unconverted` and counted nowhere:
  `unsupported` when the provider doesn't have the cost's or the trip's currency, else
  `unavailable`. No rate is guessed.
- Once the provider fails (`Fx` raises `FxUnavailableError`, answers an older day's rate, or answers
  today's as kept from before `FX_CACHE_TTL_HOURS`), budgets use only the rates already kept for
  `budget.fx_rest_seconds` (300). A budget with many days to convert then waits on the provider at
  most once.
- The rest, and the currencies the provider is known not to have, are this process's own memory. A
  currency found missing stays so until the process restarts; that only changes the reason given.
- `FxRate` joins the platform API, so the budget can read one day's kept rates.
- Who owes whom is worked out in whole minor units from actual costs with a payer. The units an
  even split leaves over go one each to the first sharers in the trip's order of travellers; the
  largest debt is then paid to the largest credit. The same records always give the same transfers.

### Premium is locked in the API; Basic AI works as Static (until Phase 19)

- A trip's `mode` (`static`, `basic`, `premium`) is one field of the trip.
- Saving `premium`, on a new trip or as a switch, answers 403 `TRAVEL_PREMIUM_LOCKED`, through one
  function, `premium_allowed(ctx)`, false for everyone. `GET /options` says so
  (`premium_available`), and the app's Premium card can't be chosen. The lock is the API's; the
  page only follows it.
- `basic` can be saved. In Phase 18 such a trip worked exactly as a Static one, and its card said
  so. **Superseded in part by Phase 19** (ADR-0026, `docs/modules/travel.md`): Basic now turns on
  the trip's AI help, which only drafts; every change it drafts still goes through the preview,
  the one event and its undo described here, the event saying AI made it (`source: ai`). A Static
  trip refuses AI (409 `TRAVEL_AI_OFF`), and so does Premium while it is locked.
- A switch of mode alone writes only the trip's own revision: no event, no feed entry, no reminder
  change (rule 7).

### Suggestions are rules, and never overwrite (rule 4)

- `data/travel.yaml` holds them: items by kind of activity, each tied to its activity and day, and
  a packing list by bag whose quantities follow the trip's length, for one person. They are common
  sense, never what a country, airline or operator requires.
- Each is stored as a suggestion (`source` `rule`) with a key for good (`activity:{id}:{rule}`,
  `packing:{rule}`). Asking again adds only keys not there yet, so an item edited, completed or
  removed is never touched. A unique index keeps two requests from adding one key twice
  (`TRAVEL_SUGGESTION_TAKEN`).
- A suggestion the person removes stays, soft-deleted and `dismissed`, so it isn't offered again.
  One taken away by an undo isn't dismissed.
- One request adds at most `suggestions.max_per_run` (100); `more` says there are others.
- Titles are stored in English. While an item keeps its rule's title (a tick keeps it so), the app
  words it by its key (`as_suggested`; `suggestion_key` on change lines and on `kept`).
- Any change the person makes turns `edited` on. Skipping, replacing or removing an activity keeps
  an edited or completed item; a skip or a replacement lists it under `kept`.

### Room on the first page: a build plugin (frontend)

- Vite writes each lazy import's list of files to fetch into the chunk that makes the import. For a
  module's pages, that is its manifest, which loads with the first page.
- `vite-plugins/preload-lists.ts` (build only, through `build.modulePreload.resolveDependencies`)
  leaves out of those lists the JavaScript files the HTML entry imports, however deep: they have
  loaded before any lazy import runs, and `index.html` preloads them. Stylesheets stay listed. This
  took `/` from 199.3 KB to 197.0 KB before Travel.
- A trip's pages load as one chunk through a small loader (`trip-loader.ts`), so their lists are
  written there, not in the manifest. Refusals only the trip's pages meet are worded in the
  module's own translations, which load with those pages. The trip's pages use other icons than
  three the first page uses: shared with lazy code, those were split into first-load chunks of
  their own.
- With Travel, `/` is at 199.9 KB of 200, and `/login` at 193.7 KB.

### Where it differs from the Phase 18 contract (§6a)

- **More in the answers**: `GET /options`; `phase` on a trip; `days` on a change; `undoable` on an
  event; `event_id` on every write; `open_days` on a day's plan; `as_suggested` on an item;
  `suggestion_key` on `kept`; the day to open on `finish_checklist`. A change and a checklist edit
  may carry `revision_no`.
- **Removals** answer 200 `{event_id}`; a trip's deletion 204 (above).
- **A mode switch alone records no event** (rule 7 read literally: every other record stays as it
  was, the history included). One sent with other fields records one.
- **A tick** (a change of state alone) records no event and can't be undone from History; the
  item's own revisions keep it. It may come without `revision_no`, so a tick made offline can be
  sent later.
- **An activity's edit** takes neither `day` nor `status`: a move to another day is a reschedule
  (which moves its items too), and its status changes only through `/change`. Only a planned
  activity changes so (409 `TRAVEL_ACTIVITY_STATE`); a mistaken done or skip is undone.
- **Rule 3, as built**: the booking an activity links to is listed under `needs_review` on a skip,
  a replacement or a reschedule; its estimates, and its own `estimated_cost_minor`, on a skip or a
  replacement. Removing an activity stands its items down as a skip does.
- **Bookings** are saved only `entered` or `confirmed`: `extracted` and `verified` are for the
  app's own reading and checking, which nothing does yet.
- **Revisioned records**: checklist items, bookings and expenses keep every revision, as trips and
  activities do, so their changes can be undone.
- **Packing quantities** are for one person, not per traveller.
- **Moves up and down** in the itinerary are saved at once, with Undo in a toast, not previewed
  first. An activity's edit shows what it changes first.
- **A trip is edited and deleted** from its Overview, in a dialog, not on a route of its own.
- **Names**: an activity's Beanie class is `TripActivity` (the platform has an `Activity`), its
  collection `travel_activities`; schema names carry `Travel` where a plain one would clash.

## Consequences

- A new kind of record joins changes and undo with a store in `events.STORES` and an effect in
  `changes.EFFECTS`; its documents go in the manifest.
- First-load JavaScript is 199.9 KB of 200 on `/`: 0.1 KB is left. The next module, or a new route
  in Travel's manifest, needs room found first. The next lever measured is loading English feed
  words and replies with their module rather than the first page (about 1.25 KB).
- The plugin uses an option Vite 8 marks experimental (`resolveDependencies`);
  `src/test/preload-lists.test.ts` checks the lists it leaves.
- Known limits (`docs/modules/travel.md`): a trip's today is its first destination's time zone;
  two creates at once can both pass a cap; only the latest change to a record can be undone;
  ticks made offline are last-write-wins; an expense with no day is converted at today's rate, so
  its figure moves until it has one; and the gaps above, with no transactions.
