"""Travel: a person's trips, each with its travellers, a day-by-day plan of activities, a checklist
and packing list, bookings and a budget, changed one scoped step at a time, with a history that says
what changed and can take it back (docs/modules/travel.md).

Made by `poe new-module` (docs/modules/new-module.md) and grown in Phase 18
(modules-usecases/Travel_Companion_Plan.md). Suggestions come from rules in data/travel.yaml. Since
Phase 19 a trip in Basic mode has AI help (ai.py, assist.py, drafts.py, tools.py, skills/ and
agents/), which only drafts: the person previews and applies each draft through the same routes and
history as their own changes. A Static trip has none. It imports only ``app.platform``, like every
module.
"""
