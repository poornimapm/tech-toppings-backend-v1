# ADR-0007: Scheduled work without a resident cron

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Render free has no cron and sleeps when idle. Recurring entries, reminders, digests and repair tasks still need to run.

## Decision

Primary path: lazy materialization during authenticated activity, guarded by `job_runs` locks so each job runs once per scope and period. Secondary path: a GitHub Actions scheduled workflow calls a token-protected `/v1/internal/jobs/*` endpoint (best effort, every 30 minutes). Retention is handled by MongoDB TTL indexes.

## Consequences

Correctness never depends on the cron. Push reminders are best effort. GitHub disables schedules after 60 days of repository inactivity, and the runbook explains how to re-enable them.

## Alternatives considered

External cron services: another account. Keeping the instance awake 24/7: burns instance hours and still provides no scheduler.
