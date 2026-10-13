# ADR-0014: Dashboard widgets, activity log and notification inbox

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** project owner (Phase 4b)

## Context

Every module will want to show figures on a shared dashboard, record what the user did, and
tell the user when something happens. If each module built its own, the dashboard would need
editing for every new module, and the three would drift apart. The admin also needs an
"all users" view that can never leak one person's records.

## Decision

**Widgets are functions a package contributes.** Any platform sub-package or module may have
a `widgets.py` with async functions `(ctx: ActorContext, period: DateRange)` decorated
`@widget("kpi" | "series" | "list")`. They are found by the same folder scan as tools and
skills (`platform/plugins.py`), and the dashboard has no list of widgets of its own.

- Three shapes only: `Kpi` (a figure and the previous period's), `Series` (one value per
  day) and `Items` (rows). A widget that returns another shape than it declared is treated
  as failed.
- A widget sends no title. Its key names it, and the client translates the key, so the
  backend holds no display text (ADR-0009).
- Widgets run in parallel, each within `DASHBOARD_WIDGET_TIMEOUT_SECONDS`. One that fails
  or is slow is logged and sent as `data: null`, and the client says so on that card. It
  never shows a made-up zero.
- A module's widgets are included only while the module is available to the user.

**The admin view is the same widgets with another scope.** `GET /v1/admin/dashboard` runs
them with `Scope.unrestricted`, so no widget is written twice. It leaves out `list` widgets:
totals across users are safe, but rows are somebody's records.

**One date-range type.** `DateRange` (`core/daterange.py`) is a pair of calendar days in the
user's time zone, with its UTC bounds, its length, and the equally long period before it
(for trends). The default and the longest allowed range are settings.

**Activity log.** `ActivityLog.record(type, module=..., **params)` stores a `type` code and
`params`, never a sentence. Entries belong to the user, are listed at `GET /v1/activity`,
and expire after `DASHBOARD_ACTIVITY_RETENTION_DAYS` (TTL index).

**Notification inbox.** `Inbox.notify(type, module=..., dedupe_key=..., **params)` is the
in-app channel of ADR-0012. A `dedupe_key` is unique per user, so the same notice is never
delivered twice however often its trigger runs. Notifications expire after
`NOTIFICATIONS_RETENTION_DAYS`.

**Shared rule for both feeds:** `params.module` is a module key, and the client shows that
module's name in the user's language.

**Launch notices are delivered when the user next loads their modules.** A user who asked to
be told about a coming-soon module gets `module.launched` the first time `GET /v1/modules`
shows it as available. That needs no scheduler and no fan-out write at launch (ADR-0007).

**`ActorContext`** (user, scope, clock, settings, app state) is what tools and widgets
receive. It replaces the agent-only `ToolContext`, so one function can serve both.

## Consequences

- A new module gets dashboard figures, activity entries and notifications by adding a
  `widgets.py` and calling two platform services. Nothing in the platform changes.
- The client must have a translation for each widget key and feed code. An unknown one is
  shown as its code, which is visible in testing rather than hidden.
- A launch notice arrives when the user opens the app, not at the moment of launch. Web
  Push (ADR-0012) can deliver it sooner later, through the same `Inbox.notify` call.
- The admin sees totals only. Looking at one user's records stays a separate, audited admin
  feature (Phase 10).

## Alternatives considered

- **A dashboard endpoint per module.** The client would make one request per module and
  the admin view would be written again for each.
- **Sentences from the server.** Simpler for the client, but breaks ADR-0009 and stores text
  in one language.
- **Writing a launch notice to every interested user when an admin launches a module.** An
  unbounded write on a free tier, and it would need a job runner.
