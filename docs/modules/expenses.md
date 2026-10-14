# Expenses module

How the module reads what people say, type or paste, and how well it does. The rest of the
module is described in [architecture.md](../architecture.md#expenses).

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
| `words.yaml` | currency, conjunctions, income and spending words, fillers, units, days, weekdays, months |
| `categories.yaml` | words that point to a system category, and the "other" category of each kind |
| `sms.yaml` | how bank and UPI messages are recognised and read |
| `lexicon.yaml` | the skill's actions, and how sure the parser is of each kind of guess |

It reads Tamil and Devanagari digits, "1,25,000", "2k", "two fifty" (250), "ढाई सौ" (250);
days such as "yesterday", "நேற்று", "कल", "last Friday", "3 दिन पहले", "5 Oct", "01/10"
(never in the future); quantities ("2 kg", "அரை கிலோ"), which stay in the title; several
items ("tea 15 and coffee 20"); income ("salary vandhuchu"); and the merchant after "at".

A category comes from the user's own habit for the same title first (`expenses_habits`),
then a keyword, then "other" (marked uncertain).

## Accuracy

`poe eval-parser` scores the parser by language and field (`--misses` lists the errors).
CI gates it (`tests/modules/expenses/test_parser.py`).

**Development set** (`tests/modules/expenses/parser_cases.yaml`, 170 inputs). The lexicons
were written alongside these inputs, so this measures what the parser knows, not how it does
on new sentences.

| | entries | amount | item | kind | date |
|---|---|---|---|---|---|
| English (50) | 100% | 100% | 100% | 100% | 8/8 |
| Tamil (40) | 100% | 100% | 100% | 100% | 6/6 |
| Hindi (40) | 100% | 100% | 100% | 100% | 6/6 |
| Mixed and Latin letters (20) | 100% | 100% | 100% | 100% | 4/4 |
| Bank and UPI SMS (20) | 100% | 100% | — | 100% | 17/17 |

Gate: amount ≥ 90% and item ≥ 75% for each language; SMS amount ≥ 98%.

**Held-out set** (`parser_cases_unseen.yaml`, 40 inputs, written without looking at the
lexicons and scored once):

| | amount | item | kind |
|---|---|---|---|
| First (blind) run, 2026-10-13 | 37/40 (92.5%) | 38/40 (95%) | 36/40 (90%) |
| After removing "aaya" / "आया" ("came") from the income words | 37/40 | 38/40 | 38/40 (95%) |

Gate: amount and item ≥ 85% overall (a floor against regressions, not a target).

Known gaps, from the held-out misses:

- a figure followed by number words: "1 lakh 20 thousand" reads as 1 lakh;
- several items with no "and" or comma: "பூ 20 பழம் 60" becomes one entry of 60;
- the merchant is found only after English "at" or "from".

**AI providers** have not been measured: no provider key is configured where this was built.
To measure one, set `AGENT_PROVIDERS`, give consent, and ask the assistant the same inputs
(`skill: track-expenses`); record the results in this table.

## Not yet

- Lending and borrowing ("gave Ravi 500", "கடன்") arrive with IOUs (Phase 9a).
- Corrections ("change the last entry to 300") are not understood yet.
- Quantity and unit stay in the title until products and unit prices (Phase 7).
