---
name: manage-modules
description: Favourite, unfavourite, show, hide or open the user's modules, and turn launch notices on or off for coming-soon modules. Use when the user wants to change, find or open something on their Welcome page.
allowed-tools: modules_list modules_set_preferences modules_set_interest modules_open
---

# Managing modules

1. Call `modules_list` first. Use only the keys it returns; never guess a key.
2. Work out which module the user means. They may use a translated, partial or misspelt
   name; the request context lists the names they see on screen, with their keys.
3. Make each change with one tool call:
   - favourite or unfavourite: `modules_set_preferences` with `pinned`;
   - show or hide on their page: `modules_set_preferences` with `enabled`;
   - launch notice: `modules_set_interest` (coming-soon modules only);
   - open: `modules_open`.
4. Changes are proposals: the user sees each one and confirms it. Give every change a short
   `summary` in the user's language, naming the module as they see it.
5. If a module is already in the state they asked for, say so instead of proposing a change.
6. If you can't tell which module they mean, ask one short question listing the closest
   matches.
