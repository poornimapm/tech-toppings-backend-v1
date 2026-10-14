---
name: explain-spending
description: Sum up the user's spending for a week or a month in a few friendly sentences, from their own figures - what went out and came in, how it compares with the same days before, where most of it went, and anything worth noticing. Use when they ask how their week or month went, or for a summary or insights of their spending.
allowed-tools: expenses_insight_facts
---

# Explaining spending

1. Call `expenses_insight_facts` once. A page asks with the period and its first day, such
   as "month 2026-10-01". Otherwise use the month (or the week, if they say week) that
   contains today.
2. Write two to four short sentences in the user's language and script. Use only figures the
   tool gave you: never estimate, add up or invent one. Write amounts the way people say them
   in that currency (₹2,450, not ₹2449.73).
3. Start with what was spent. When `spent_change_pct` is given, compare it with the same days
   of the period before ("12% more than the same days last month"); when it is missing there
   is nothing to compare with, so don't.
4. Say where most of it went (`top_categories`). Category ids such as `groceries` or
   `phone_internet` are words to put naturally in the user's language.
5. Add one thing worth noticing, if there is one: an unusual expense (`unusual`), a price that
   moved (`price_moves`), what was kept (`net`, `savings_rate_pct`), or, while the month isn't
   over (`partial`), where it is heading (`forecast.projected`).
6. Be warm and plain. No judgement, no lecture; at most one small, practical suggestion.
7. If nothing was spent, say so in one sentence.
