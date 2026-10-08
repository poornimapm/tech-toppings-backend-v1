# ADR-0004: Money as integer minor units; calendar dates; one ledger with kind

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** project owner (plan approved 2026-10-08)

## Context

Floating-point money loses cents. Multi-currency totals must be exact and stable over time. Users think in calendar days (yesterday), not UTC instants. Income (U1) should share analytics with expenses.

## Decision

Store `amount_minor` (int) plus an ISO-4217 `currency`. Each entry snapshots `fx_rate` and `amount_base_minor` in the user's base currency at entry time. `date` is a calendar date with an optional local `time`. Entries carry `kind` = `expense` or `income`.

## Consequences

Sums are exact integers. Historic totals don't move when exchange rates change. Frontend formatting goes through `Intl.NumberFormat`. Conversions happen in one helper (`core.money`).

## Alternatives considered

Decimal128 everywhere: heavier API and serialization. Floats: rounding errors. A separate income collection: duplicated analytics.
