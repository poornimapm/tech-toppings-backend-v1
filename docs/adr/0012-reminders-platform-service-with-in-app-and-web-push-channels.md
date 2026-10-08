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
