---
name: tidy-products
description: Find products and purchases the user follows under different names that are the same thing (another language or script, another spelling, a plural), for the user to combine. Use when they ask to tidy, combine or find duplicate products or prices.
allowed-tools: expenses_products expenses_items expenses_product_matches
---

# Tidying products

A product groups the user's purchases by name so its price can be followed. The same thing
logged under two names ("Tomato" and "Thakkali") splits its price history in two.

1. Call `expenses_products` and `expenses_items`.
2. Look for names that are the same thing:
   - the same item in another language or script: tomato, thakkali, தக்காளி and टमाटर;
   - another spelling or a plural: onion and onions, thakkali and takkali.
3. Never match different things, even when the words overlap: tomato and tomato sauce, milk
   and milk powder, rice and rice flour, a fruit and its juice. Never match two products when
   either lists the other in `apart`: the user said they differ.
4. If something matches, call `expenses_product_matches` once. In each match, keep the
   product bought most often (`product_id`) and list the other products (`products`) and the
   titles from `expenses_items` (`titles`) that are the same thing. Leave out anything you
   are unsure of.
5. Nothing is combined until the user confirms each match. Say briefly what you found, or
   that nothing looks alike (then don't call the tool).
