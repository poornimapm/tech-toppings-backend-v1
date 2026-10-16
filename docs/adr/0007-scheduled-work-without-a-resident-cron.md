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

## Implementation notes (Phase 9a, 2026-10-16)

- `app/platform/jobs`: a package declares jobs in its own `jobs.py` with `@job("daily")` (once
  per user and local day) or `@job("each")` (every catch-up; cheap when nothing is due). Names
  start with the package key, as tools and widgets do. A module's jobs run only for users who
  have it switched on.
- "Authenticated activity" is the bell: `GET /v1/notifications` catches the caller up before it
  reads the inbox, each job within `JOBS_LAZY_TIMEOUT_SECONDS`. A slow or failing job is logged
  (`job_failed`) and never fails the poll.
- A daily job's lock is a lease row in `job_runs` (user, job, local day) held for
  `JOBS_LEASE_SECONDS`, which is also the backoff after a failure. Settings refuse a lease
  shorter than either time limit. Rows expire after `JOBS_RETENTION_DAYS` (TTL).
- The lease only saves work: exactly-once rests on each effect's own guard (a unique client key,
  a dedupe key or a guarded update), so a lost lease or a double run changes nothing.
- `POST /v1/internal/jobs/{job}/run` (`all` or one name) is left out of the OpenAPI schema and
  checks `X-Job-Token` against `JOBS_TOKEN` in constant time; with no token set it answers 401
  to every call. It catches up users signed in within `JOBS_ACTIVE_DAYS`, most recent first, at
  most `JOBS_MAX_USERS_PER_RUN`, each job within `JOBS_RUN_TIMEOUT_SECONDS` (claim, run and
  finish together), and answers with counts only.
- `.github/workflows/jobs-cron.yml` calls it every 30 minutes; it reads the URL and token from
  repository secrets and does nothing until both are set.
