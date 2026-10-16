# ADR-0015: Products group entries by name

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** project owner (Phase 7)

## Context

To follow what something costs, the purchases of one thing must be found together, although
people log it under different names: "Tomato", "tomato ", "Thakkali", "தக்காளி". The plan
(§5.2) put a `product_id` on each entry. Then every merge, and every name learned later,
would rewrite past entries; each rewrite is a revision (ADR-0002), so a user's history would
fill with changes they never made, and restoring an old revision could point an entry back
at a product that no longer exists.

## Decision

**A product is a set of names; an entry belongs to the product whose names hold its title.**

- `expense_products` keeps a display `name` and `aliases`: titles normalised as the agents
  compare text (NFKC, case-folded, single spaces). A unique index on `{user_id, aliases}` lets
  each name belong to one live product.
- Nothing on the entry points to a product. Which product an entry belongs to is worked out
  when prices are read, from the titles of the user's expenses. Combining two products moves
  the names; teaching a product a name moves that name. Entries and their history never
  change.
- **Quantity and unit** are fields of the entry (`data/units.yaml`: mass, volume, count, each
  unit with its factor to the dimension's first unit). A price per unit is worked out when
  read, in the product's `price_unit`, so changing the unit never touches stored data.
- **Products start on their own** for an expense with a quantity whose name no product has.
  Anything else is followed by asking ("Track price"). Stopping a product keeps its names in
  `stopped`, so they aren't followed again on their own.
- **Finding the same thing under two names** is the `tidy-products` skill (ADR-0013): a model
  in agent mode; in static mode, word groups (`references/products.yaml`) and spelling
  similarity (`difflib`, after English plurals). It hands suggestions to the page
  (`expenses_product_matches`); the user combines, says "not the same" (kept in `apart`, never
  suggested again), or skips.
- **A price alert** is a limit per `price_unit` on the product. An expense saved above it
  sends one notification (`dedupe_key` per entry), however often that entry is edited.

## Consequences

- Merging, renaming and learning names are single-document writes; nothing to back-fill.
- Reading prices first lists the user's distinct expense titles. That is fine at personal
  scale (thousands of entries); a `{user_id, title}` index or a stored normalised title can
  come later without changing the API.
- Two different things logged under exactly the same title are one product. The user can
  rename one of the entries' titles; suggestions never combine titles on their own.
- Entries in another currency are left out of prices: a price is compared in one currency
  (multi-currency entries exist since Phase 9b; see ADR-0004's notes).
