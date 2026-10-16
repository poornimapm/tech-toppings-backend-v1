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

## Implementation notes (Phase 9b, 2026-10-16)

- A new entry may be in any currency the rates provider has (`app/platform/fx`: Frankfurter,
  free and keyless, cached in `fx_rates` per currency and day). It keeps `base_currency`,
  `fx_rate` (a decimal string: how much base one unit bought on the entry's day) and
  `amount_base_minor`, worked out by `core.money.convert`. An edit keeps the currency it was
  made in; its snapshot is worked out again from the same day's kept rate, so it doesn't move.
- Totals count the snapshot: `sums()` adds `amount_base_minor` and groups `currency` by the
  currency an entry counts in, and the overview's criteria and the query compiler keep entries
  counted in the base currency (`in_base`). Entries from before snapshots existed count as their
  own amount in their own currency, through `$ifNull`, so no data migration was needed.
- A past day's rates are fetched once; today's again after `FX_CACHE_TTL_HOURS`. When the
  provider can't be reached, the newest kept rates up to that day are used; with none kept, the
  save is refused (503 `FX_UNAVAILABLE`), never guessed.
- Not yet: prices (ADR-0015), duplicate checks and the CSV import compare base-currency entries
  only.
