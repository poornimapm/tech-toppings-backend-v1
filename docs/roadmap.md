# Roadmap

What exists, what comes next, and how the upcoming modules get built (Phase 13). The phase
history and every decision are in [`plan.md`](plan.md); this page looks forward.

## Where things stand (2026-10-29)

- **Platform**:
  - accounts (allowlist, Google, two-step sign-in), the module system, the agents runtime with
    skills;
  - dashboard, activity, notifications, reminders, jobs, search;
  - privacy (export, erasure), the admin console, the audit trail;
  - the installable offline app with Web Push, Simple mode, en/ta/hi throughout.
- **Expenses**, the first module (beta): entries by voice or text, budgets, goals, bills, loans,
  prices, reports, import/export, insights.
- **Deployment** is prepared (`deployment.md`). Going live, and the checks only a live site
  allows, are the owner's step.
- **New modules** come from one command, `poe new-module` (`modules/new-module.md`).

## Next

1. **Go live** (`deployment.md`), then the live checks:
   - the smoke check;
   - Lighthouse on the live address;
   - a push on Android from the cron.
2. **Phase 14, receipt scan preview (U10)**:
   - a photo of a receipt becomes draft expenses through the same confirm card;
   - with AI (Gemini, in memory, nothing stored) or a fallback with AI off;
   - accuracy measured on 20 sample receipts.
3. **The upcoming modules**, below, in the order the owner chooses. Each *Notify me* press is
   stored per person, on their preferences for that module (`user_modules`,
   `notify_requested_at`; the plan's separate `module_interest` collection wasn't needed). The
   admin console doesn't count them yet: adding that would help choose.

## The seven upcoming modules

Each is already a tile marked *Coming soon*, with *Notify me*. The first slices below are ideas
to start from, not decisions.

| Module | What its tile promises | A first slice |
|---|---|---|
| Medicine | Doses on time, refills before you run out | Medicines with a dose schedule; a reminder per dose (platform reminders) |
| Receipts & warranty | Every bill, findable in seconds | Receipts with a warranty end date and a reminder before it; scanning comes with Phase 14 |
| Study helper | Plan, revise, remember | Topics with a next-revision date that moves out with each revision |
| Government documents | Renewals without the runaround | Documents with an expiry date and reminders before it |
| Meal planner | What's cooking this week? | A week's menu; a shopping list from it |
| Habit tracker | Small steps, every day | Habits checked off each day, with streaks |
| Journal | A private space for your thoughts | Entries written or spoken, searchable, private |

**Building one.** Its folders exist as *coming soon*, and `poe new-module` never writes over a
folder, so:
1. Write its spec (`modules/example-module.yaml` shows the shape), from its two manifests:
   - from `app/modules/<key>/manifest.py` here: the key and the order;
   - from `src/modules/<slug>/manifest.ts` in the frontend: the icon, the accent, the keywords,
     and the name, tagline and description in en/ta/hi;
   - new, in en/ta/hi, as a coming-soon module has no page yet: the tile figure's label
     (`items`), the add button, the field label and the empty list's text;
   - the status: `beta` until it has been used for a while.
2. Delete its two coming-soon folders: `app/modules/<key>` here, and `src/modules/<slug>` in the
   frontend.
3. Run `poe new-module --spec <file>`, then grow the module (more fields, reminders, search,
   widgets, agent skills), as Expenses did.
4. Run the whole backend suite (`poe test`), not only the checks the generator runs. A few
   tests use an upcoming module by name for *Notify me* and its launch notice, and need
   another upcoming module once theirs is built:
   - `receipts`: `tests/platform/notifications/test_notifications.py`,
     `tests/platform/dashboard/test_activity.py`,
     `tests/platform/agents/test_static_manage_modules.py`;
   - `habits`: `tests/platform/agents/test_runs.py`.

   The catalog tests pick their upcoming example from discovery, so they need no change.
5. Add a page for it in `docs/modules/<key>.md`.

## Platform follow-ups (open)

- **Re-pin `certifi`**: `pip-audit` flags 2026.7.22, fixed in 2026.10.19. `poe lock` is the
  owner's call.
- **Frankfurter v2**: v1, which the app uses, is deprecated but still answers. Moving needs a
  small adapter change (`free-tier-limits.md`).
- **"New device signed in" notice** (D15): still open.
- **Finishing a failed erasure**: an operator script for an erasure that failed after the soft
  delete. ADR-0017 has the manual steps.
- **Phones**: the Play Store (a Trusted Web Activity) and iPhone (a Home Screen web app) paths
  are in [`mobile.md`](mobile.md). The Play Store one costs a registration fee.
