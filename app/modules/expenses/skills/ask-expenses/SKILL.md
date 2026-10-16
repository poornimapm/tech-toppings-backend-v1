---
name: ask-expenses
description: Answer questions about the user's own spending and income with their real figures - how much they spent on something or somewhere, in a period, how many times, on average, the biggest expense, where most of it went, who it was for, month by month. Use for questions like "how much did I spend on groceries last month?" or "where did my money go this week?".
allowed-tools: expenses_query expenses_categories
---

# Answering questions about spending

1. Work out the question as figures: the metric (total, count, average or largest), whether it
   is money spent (kind expense, the usual) or received (income), the days (today is in the
   prompt; "last month" is the whole month before this one), and any grouping ("where" is by
   merchant, "on what" by category, "who for" by person, "each month" by month).
2. For a category the user names, call `expenses_categories` and use the ids that fit
   (several are fine: "food" can be both `food` and `groceries`).
3. Call `expenses_query` once with that query. Ask for at most the rows you will mention.
4. Answer in one or two short sentences in the user's language and script, with the figures
   the tool returned and nothing else: never estimate, add up or invent a number. Write money
   the way people say it in that currency (₹2,450). Say the period you looked at.
5. If the tool finds nothing, say so plainly and name the period you looked at.
6. If the question isn't about the user's own figures (advice, prices elsewhere, the news),
   say briefly that you can answer questions about their own entries.
