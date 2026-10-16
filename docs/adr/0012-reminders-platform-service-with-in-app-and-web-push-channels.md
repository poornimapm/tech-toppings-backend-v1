# ADR-0012: Reminders platform service with in-app and Web Push channels

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Bills (U3), IOU due dates (U2) and, later, medicine doses need reminders. The backend sleeps when idle.

## Decision

`platform.reminders` stores due and remind times and delivers through `platform.notifications`. The in-app channel is always available. The Web Push channel (VAPID, opt-in, from Phase 11) is dispatched by the cron-triggered job endpoint.

## Consequences

Reusable across modules. In-app reminders are reliable. Push is best effort, and on iOS only works for installed PWAs (16.4+).

## Alternatives considered

Module-specific reminder code: duplication. Paid push or SMS services: not free.

## Implementation notes (Phase 9a, 2026-10-16)

- In-app channel only; Web Push follows in Phase 11 on the same rows.
- `Reminders(ctx).schedule(type, module=, ref_id=, due_on=, days_before=, **params)` and
  `.cancel(module=, ref_id=)`. One row per (module, record, due date); a record has one live
  reminder, so a new due date cancels the others and the later of two writes wins.
- A reminder is told at `REMINDERS_SEND_HOUR` local time, `days_before` ahead of the due date
  (at once if that moment has passed). Changing time zone keeps each untold reminder's local day
  and hour.
- Exactly once per due date: delivery first notifies with the inbox `dedupe_key`
  `reminder:{id}:{round}`, then marks the row sent with a guarded update. A crash between the
  two re-sends into the same dedupe key, which the inbox ignores. A sent or dismissed row is
  never re-armed; a snooze starts a new round.
- Delivery is the platform's `each` job `reminders_deliver`, so the bell's poll and the cron
  both deliver; tests run it repeatedly and concurrently and see one notification.
- `GET /v1/reminders`, `POST /v1/reminders/{id}/snooze` (at most `REMINDERS_MAX_SNOOZE_DAYS`) and
  `/dismiss`. Rows are kept `REMINDERS_RETENTION_DAYS` after the due day; a due date older than
  that is never scheduled again.
- First users: bills (`expenses.bill_due`) and loans (`expenses.iou_due_lent`,
  `expenses.iou_due_borrowed`).
