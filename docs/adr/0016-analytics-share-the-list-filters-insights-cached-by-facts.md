# ADR-0016: Analytics share the list's filters; insights are cached by their facts

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** project owner (Phase 8)

## Context

The expenses overview has twelve figures and charts, and each must open the entries behind
what was clicked with the same total (plan, Phase 8). Separately written queries for the
charts and the list would drift apart. The overview also shows an AI-written summary, and a
free-tier AI quota can't afford a call on every visit, while the figures change rarely.

## Decision

**Every analytics endpoint takes the entries list's own filter model** (`EntryFilters`:
`from`, `to`, `kind`, `category_id`, `for_whom`, `merchant`, ...) and adds up the same
records with the same query builder (`EntryFilters.criteria()` and `sums`). A chart's mark
links to the list with those filters, so the list's totals equal the clicked value by
construction; tests check it for categories, people, places and days. The list gained exact
`merchant` filtering and custom `from`/`to` ranges in its URL for this.

- Tuning (forecast lookback and band, anomaly threshold, top-N, months shown) is data:
  `data/analytics.yaml`.
- **Forecast:** spent so far, plus the days left at the average day of the recent lookback
  (from the first expense when there is less history); the band uses that span's quieter and
  busier whole weeks (percentiles of each week's average day).
- **Comparisons:** a span that starts a month against the same days of the month before;
  any other span against as many days just before it.
- **Anomalies:** an expense far above the user's usual for its category over the lookback:
  robust score (median and median absolute deviation) at or above the threshold, at least a
  minimum ratio of the median, with enough history.

**Insights are facts plus a skill, with the cache in front.** `GET /insights` works out the
period's facts (aggregates and the user's own labels only), hashes them and returns the kept
AI summary while the hash is unchanged. Otherwise it runs the `explain-spending` skill with
`run_skill` (a platform helper that runs one skill with the usual mode, fallback, quota and
consent). Only AI summaries are kept: without AI the skill's static templates answer, which
cost nothing and so are written fresh, and turning AI on is seen at once.

## Consequences

- A second look at an unchanged period makes no provider call (tested).
- The facts include today's date and a forecast while the period is open, so a summary is
  written at most once a day per period and language while the user keeps looking.
- `POST /insights/refresh` writes a new summary on request (it uses quota).
- No background precompute job: summaries are written on first view. A scheduled job can
  call the same service later without changing the API.
