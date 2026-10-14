# Expenses module

How the module reads what people say, type or paste, how well it does, and how it follows
prices. The rest of the module is described in [architecture.md](../architecture.md#expenses).

## Capture

1. The user types, dictates (the browser's speech recognition) or pastes a bank SMS on the
   Expenses page. The text stays editable until they send it.
2. The page asks the assistant agent with the `track-expenses` skill
   (`POST /v1/agents/assistant/runs`, `context.input_type` = `text` or `speech`).
3. The agent runtime decides who reads it, by the usual switches (ADR-0013):
   - **AI** (consent, quota and a provider): the model calls `expenses_categories` and
     `expenses_habits`, then `expenses_draft`;
   - **rules** (`AGENT_MODE=static`, or AI unavailable with `AGENT_FALLBACK=true`): the
     skill's static script runs the rule parser (`app/modules/expenses/parsing`);
   - **neither** (`AGENT_FALLBACK=false`): an honest "unavailable" with the reason.
4. Either way the answer is the client action `expenses_draft`: one draft per thing bought,
   with the fields that were guessed listed in `uncertain`. Nothing is written.
5. The confirm card shows the drafts. Complete ones can be saved as they are; one with a
   missing amount, title or category can only be opened in the entry form and completed.
   Each draft saves once, with its own `Idempotency-Key`.
6. Optionally the saved entry is read aloud, with a device voice for the user's language
   when there is one.

The general assistant (the command palette) uses the same skill. There, a sentence becomes
drafts only when it speaks of money and has an amount ("buy milk" is a to-do), and the
answer offers to open Expenses with the drafts.

## The rule parser

Everything it knows is data in `skills/track-expenses/references/`:

| File | What it holds |
|---|---|
| `numbers.yaml` | number words, multipliers ("lakh", "ஆயிரம்", "सौ") and Hindi shifts ("साढ़े", "पौने") |
| `words.yaml` | currency, conjunctions, income and spending words, fillers, the words for each unit, days, weekdays, months |
| `categories.yaml` | words that point to a system category, and the "other" category of each kind |
| `sms.yaml` | how bank and UPI messages are recognised and read |
| `lexicon.yaml` | the skill's actions, and how sure the parser is of each kind of guess |

It reads Tamil and Devanagari digits, "1,25,000", "2k", "two fifty" (250), "ढाई सौ" (250);
days such as "yesterday", "நேற்று", "कल", "last Friday", "3 दिन पहले", "5 Oct", "01/10"
(never in the future); quantities ("2 kg", "அரை கிலோ", "a dozen"), as a quantity and a unit
code of `data/units.yaml`; several items ("tea 15 and coffee 20"); income ("salary
vandhuchu"); and the merchant after "at".

A category comes from the user's own habit for the same title first (`expenses_habits`),
then a keyword, then "other" (marked uncertain).

## Accuracy

`poe eval-parser` scores the parser by language and field (`--misses` lists the errors).
CI gates it (`tests/modules/expenses/test_parser.py`).

**Development set** (`tests/modules/expenses/parser_cases.yaml`, 174 inputs). The lexicons
were written alongside these inputs, so this measures what the parser knows, not how it does
on new sentences.

| | entries | amount | item | kind | date | quantity |
|---|---|---|---|---|---|---|
| English (52) | 100% | 100% | 100% | 100% | 8/8 | 6/6 |
| Tamil (41) | 100% | 100% | 100% | 100% | 6/6 | 4/4 |
| Hindi (41) | 100% | 100% | 100% | 100% | 6/6 | 4/4 |
| Mixed and Latin letters (20) | 100% | 100% | 100% | 100% | 4/4 | 1/1 |
| Bank and UPI SMS (20) | 100% | 100% | — | 100% | 17/17 | — |

Phase 7 added four inputs with units (packets, grams, a litre, a dozen) and labelled the
quantity of every input that has one.

Gate: amount ≥ 90% and item ≥ 75% for each language; SMS amount ≥ 98%.

**Held-out set** (`parser_cases_unseen.yaml`, 40 inputs, written without looking at the
lexicons and scored once):

| | amount | item | kind |
|---|---|---|---|
| First (blind) run, 2026-10-13 | 37/40 (92.5%) | 38/40 (95%) | 36/40 (90%) |
| After removing "aaya" / "आया" ("came") from the income words | 37/40 | 38/40 | 38/40 (95%) |
| Phase 7: quantities labelled on the two inputs that have one | 37/40 | 38/40 | 38/40 (95%); quantity 2/2 |

Gate: amount and item ≥ 85% overall (a floor against regressions, not a target).

Known gaps, from the held-out misses:

- a figure followed by number words: "1 lakh 20 thousand" reads as 1 lakh;
- several items with no "and" or comma: "பூ 20 பழம் 60" becomes one entry of 60;
- the merchant is found only after English "at" or "from".

**AI providers** have not been measured: no provider key is configured where this was built.
To measure one, set `AGENT_PROVIDERS`, give consent, and ask the assistant the same inputs
(`skill: track-expenses`); record the results in this table.

## Products and prices

A product is something the user buys again and again, known by one or more names
(ADR-0015). An expense belongs to the product whose names hold its title, so "Tomato",
"tomato" and, once learned, "Thakkali" are one product.

- **Starting.** An expense with a quantity ("1 kg tomato 40") starts a product when no
  product has its name yet. Anything else can be followed from its entry page ("Follow its
  price"); without a quantity, each purchase counts as one ("coffee 25" is ₹25 a cup).
- **Price per unit.** Worked out when read, in the product's price unit: grams are priced per
  kg and ml per litre; a unit of one or more is priced per itself (per dozen, per packet).
  The user can choose another unit of the same kind (`PATCH price_unit`).
- **What is shown** (`GET /v1/expenses/products`, `GET /v1/expenses/products/{id}`): every
  purchase with its price per unit, lowest / highest / average / last, the change since the
  purchase before, each place's average, and the cheapest place among those noted.
- **Names.** A product's names can be edited; a name another product has is refused with that
  product's id (combine them instead). Merging moves the other product's names over;
  stopping gives the names up and keeps them from starting a product again on their own.
- **Finding duplicates** is the `tidy-products` skill. In agent mode a model reads
  `expenses_products` and `expenses_items` (titles nothing follows yet) and suggests matches;
  in static mode the same comes from `references/products.yaml` (one group per thing, in
  English, Tamil and Hindi) and spelling (`references/lexicon.yaml`: similarity 0.9 after
  English plurals: onion/onions and thakkali/thakali match, rice/price doesn't). The user
  confirms each match; "not the same" is remembered (`apart`).
- **Price alert.** A limit per price unit. An expense above it sends the notification
  `expenses.price_alert` once per entry; the notification leads to the product.

Acceptance (plan, Phase 7) is a test: "1 kg tomato ₹40 @A", "500 g tomato ₹25 @B" and
"thakkali 2 kg ₹70", once Thakkali is combined into Tomato, are one product charted at ₹40,
₹50 and ₹35 per kg (`tests/modules/expenses/test_products.py`).

## Overview and insights

Every chart on the Overview page is an endpoint under `/v1/expenses/analytics/` that takes the
entries list's own filters (ADR-0016). Clicking a bar, a row or a day opens the list with the
same filters, and its totals match what was clicked; `test_analytics.py` checks this for
categories, people, places and days on seeded data with known values.

- **Comparisons** (summary, running total): a period that starts a month is compared with
  the same days of the month before (1-14 October against 1-14 September, as the insights
  do); any other span with as many days just before it.
- **Forecast**: spent so far plus the days left at the average day of the last
  `forecast.lookback_days` (from the first expense when there is less), with a band from
  that span's quieter and busier weeks (`forecast.band_percentiles` of each whole week's
  average day: shopping once a week makes most single days zero).
- **Unusual spending**: an expense at least `anomalies.threshold` robust deviations above the
  median of its category over `anomalies.lookback_days`, at least `anomalies.min_ratio`
  times that median, given `anomalies.min_history` earlier expenses.

**Insights.** `GET /v1/expenses/insights?period=month&date=...` works out the facts of the
week or month so far (against the same days of the one before), then asks the
`explain-spending` skill for two to four sentences. The AI is given the facts in the
currency's main unit and only aggregates and the user's own labels. Its summary is kept with
the hash of the facts and returned again (`cached: true`) until they change. Without AI, the
skill's templates (`explain-spending.<period>.<more|less|same|first|empty>`, worded in the
frontend manifest) say the same in one or two sentences.

## Not yet

- Lending and borrowing ("gave Ravi 500", "கடன்") arrive with IOUs (Phase 9a).
- Corrections ("change the last entry to 300") are not understood yet.
- Prices are in the user's base currency; entries in other currencies wait for exchange
  rates.
