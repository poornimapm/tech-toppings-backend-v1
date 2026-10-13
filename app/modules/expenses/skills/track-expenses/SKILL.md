---
name: track-expenses
description: Record money the user spent or received, look up their recent spending, or open their expenses. Use when they mention buying, paying, spending, earning or being paid, or ask about their expenses.
allowed-tools: expenses_categories expenses_list expenses_add expenses_open
---

# Tracking expenses

1. To record something, call `expenses_categories` first and pick the closest category of the
   right kind (expense or income). Use only ids it returns; when nothing fits, use the
   "other" category of that kind.
2. Call `expenses_add` once per item the user mentions:
   - `amount` is a positive number in their currency's main unit, exactly as they said it;
   - `on` is the day it happened: work it out from today's date for "yesterday" and the
     like, and leave it out when they give no day;
   - fill `merchant` and `for_whom` only when they said so.
3. Each entry is a proposal the user confirms. Give it a short `summary` in their language,
   with the amount and what it was for.
4. Never guess an amount. If it is missing or unclear, ask one short question instead.
5. For "what did I spend" questions call `expenses_list` (with days when they name a period)
   and answer from its results only, with totals you add up yourself.
6. To show their entries, call `expenses_open`.
