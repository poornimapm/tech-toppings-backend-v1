# ADR-0020: A module generator, and the paths to the app stores

- **Status:** Accepted
- **Date:** 2026-10-29
- **Deciders:** project owner (Phase 13)

## Context

Phase 13 asks for a generator (`new_module.py`, `new-module.ts`), the upcoming modules'
manifests, a roadmap, and the paths to the Play Store (TWA) and iPhone (PWA). It's done when
one command creates a working empty module whose tile, route and test all pass.

A module spans two repositories:
- the backend: a manifest, data, routes, tests, and a place in the isolation route matrix;
- the frontend: a manifest with texts in en/ta/hi that the translation check reads, pages,
  i18n files, and API types generated from the backend.

The seven upcoming modules already ship as *coming soon* manifests on both sides.

The owner chose (2026-10-29):
- a working module with one record type and add, change, delete, rather than a bare skeleton;
- a spec file for the Tamil and Hindi texts;
- one command for both repositories.

## Decision

### One spec, one command

- A YAML spec holds:
  - the module's key, icon (a lucide-react name), accent, status (`live` or `beta`), Welcome
    order and keywords;
  - its texts in en, ta and hi: name, tagline, description, the tile figure's label, the add
    button, the field label, and the empty list.
  - `docs/modules/example-module.yaml` shows the shape. It is checked before anything is
    written, including the same "not left in English" rule as the frontend's translation check.
- `poe new-module --spec <file>` (`scripts/new_module.py`) does it all:
  1. checks the spec on both sides (the frontend's generator with `--check` knows the icons and
     colours), and that no module of that key is there;
  2. writes the backend module and its tests, in the project's style (imports sorted,
     formatted);
  3. exports the API schema;
  4. runs the frontend's `scripts/new-module.ts` (Node runs TypeScript itself) on the spec as
     JSON;
  5. regenerates the frontend's API types;
  6. runs the new module's tests with the shared checks it joins on both sides: discovery,
     isolation, architecture, the registry, translations, types and lint.
  It never writes over a folder, and if a step fails (or is stopped) before the checks it
  removes what it wrote, on both sides, and restores the generated API files. Each script also
  runs alone (`--backend-only`, which still asks the frontend's check when the frontend is
  there; `npm run new-module`).

### What a new module is

- **One record type**: an *item* with a short text (1 to 200 characters), owned by the
  signed-in user. Every route is scoped to that user (`UserScope`), so another user's items
  answer 404. Export and erasure include them with no extra code (ADR-0017).
- **Backend**: the plan's flat layout (the repository beside its document in `models`, the
  tests in `tests/modules/<key>/`):
  - `manifest`, `models` (collection `<key>_items`), `schemas`, `service`, `router` (CRUD under
    `/v1/<slug>/items`), `stats` (the tile counts the items);
  - schema names carry the module (`<Key>ItemOut`), so two generated modules never share an API
    type.
- **Frontend**:
  - the manifest with the page route and the spec's texts;
  - TanStack hooks over the typed client, the list a page at a time (*Show more*);
  - a page with the list, an add form, and changing or deleting in place, where focus follows
    for keyboard users (after a delete too), busy buttons stay focusable, and a delete asks
    first with Cancel focused;
  - its i18n files, whose generic words (save, delete …) the generator carries in all three
    languages;
  - a test with an axe check.

### Checks that take a new module as it comes

Some checks used to list the real modules by name, so a new one would have failed them. They no
longer do:
- **The route matrix** loads each module's own factories from
  `tests/modules/<key>/isolation.py` (`PREFIX`, `RESOURCES`), matched by route prefix, so two
  modules may both name their id `item_id`.
- **The catalog and discovery tests** read the module list from discovery. They still check
  that the seven shipped modules are there, and that a module is upcoming exactly when it has
  no routes; the catalog tests take their upcoming example from discovery.
- **The frontend registry test** checks that the shipped modules are there.
- **Ruff** names the project's own packages (`known-first-party`), so a file about to be
  written sorts like one already there.
- **The generator's own tests**:
  - in CI, the backend test writes a module into a temporary package and drives it through the
    real app; its files are linted as if in place;
  - the frontend test runs the generator into a temporary folder: what it writes, what it
    refuses.

### The upcoming modules and the stores

- The seven *coming soon* manifests stay as they are. Building one means moving its texts into
  a spec, deleting its two folders, and generating (`roadmap.md`), so the generator never needs
  to merge into existing code.
- `docs/roadmap.md` holds what's next, a first slice for each upcoming module (ideas, not
  decisions), and the open platform items.
- `docs/mobile.md`:
  - **Android**: a Trusted Web Activity made with Bubblewrap, Digital Asset Links from
    `public/.well-known/assetlinks.json`, and the Play listing (a registration fee, the only
    cost in the project).
  - **iPhone**: stay a Home Screen web app. The App Store costs a yearly fee, and usually
    rejects a website wrapper.

## Consequences

- No new dependencies. Node 24 (already required) runs the frontend generator as TypeScript.
- A generated module is a starting point: one text field. Growing it (more fields, reminders,
  search, widgets, skills) is ordinary work, as in Expenses.
- The generator's templates are code too. Their tests keep them working and in the project's
  style, but a change to the platform's module API means changing them as well.

## Alternatives considered

- **A bare skeleton**: less to learn from, and the owner preferred a working list.
- **Texts as command-line options**: long commands, and Tamil and Hindi are hard to type in a
  Windows terminal.
- **One command per repository**: two steps to forget in between (the API types).
- **Upgrading a coming-soon module in place**: merging into existing files is risky for the
  little it saves.
