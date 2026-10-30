# A new module in one command

`poe new-module --spec <file>` creates a working module in both repositories: the backend
module and its tests here, the frontend module and its test in `../tech-toppings-frontend-v1`.
It also updates the frontend's API types and runs the new module's tests on both sides
(Phase 13, ADR-0020).

## 1. Write a spec

Copy [`example-module.yaml`](example-module.yaml) and change every value:
- the module's key, icon, colour, status (`live` or `beta`), place on the Welcome page and
  search words;
- its texts in English, Tamil and Hindi: name, tagline, description, the tile figure's label,
  the add button, the record's field label, and the empty list's text.

The spec is checked before anything is written, every problem at once.
- **The key** names the module everywhere: the folders, the collection (`<key>_items`), the
  API path (`/v1/<slug>`, where the slug is the key with `-` for `_`) and the types (`PlantsItemOut`).
  - It is 2 to 24 characters: lowercase letters and digits, in parts joined by single `_`, each
    part starting with a letter. `house_plants` works; `House`, `house__plants`, `plants_` and
    `plants_2` don't (the last two would make the same type names as `plants` and `plants2`).
  - Some keys that look right are refused too:
    - a Python keyword, such as `class` or `import`;
    - a path the platform has under `/v1` (`admin`, `auth`, `me`, `modules`, `search` ...:
      `RESERVED` in `scripts/new_module.py`);
    - a module already there on either side, the *coming soon* ones included (see
      [`roadmap.md`](../roadmap.md) for building one of them), or the test module (`notes`).
- **The icon** is a lucide-react icon's name, by any of its names (`Sprout`, `SproutIcon`).
  Three icons share a name with JavaScript's own (`Map`, `File`, `Infinity`): use `MapIcon` and
  so on. **The colour** is one the design system has (`ACCENTS` in the frontend's
  `src/core/modules/types.ts`). The backend checks their shape and the frontend's generator
  checks them for real (`--check`), still before anything is written. With `--backend-only`
  that check runs when the frontend repository (with Node and its packages) is there, and it
  says so when it can't: then the icon is checked when the frontend is made. lucide-react's
  deprecated icons are refused too: the project's lint refuses them.
- **The texts**: all three languages, every text, none empty or too long (name 40 characters,
  tagline 80, description 300, the figure's label 30, the button and the field 40, the empty
  list 120). Tamil and Hindi must be translated: a sentence left as the English is refused, as
  the frontend's translation check would. Spaces are tidied: one between words, none around.

## 2. Run it

```powershell
.venv\Scripts\poe new-module --spec docs\modules\plants.yaml
```

| Option | What it does |
|---|---|
| `--frontend <folder>` | The frontend repository (default `../tech-toppings-frontend-v1`) |
| `--backend-only` | Write only the backend module (then run the frontend generator yourself) |
| `--no-check` | Skip the tests at the end |

It refuses to write over anything. Then it runs the new module's tests and the shared checks it
joins:
- backend: its tests, isolation, architecture, module discovery;
- frontend: types, lint, its test, translations, the registry.

At the end it prints what it made. It takes a few minutes, most of them the checks.

### If something fails

- **While writing** (a step fails, or you press Ctrl+C): it removes what it wrote, on both
  sides, and puts back `openapi.json` here and `src/core/api/schema.d.ts` in the frontend. It
  says so, or names any folder it couldn't remove (delete that by hand).
- **In the checks at the end**: the files are kept, so you can see what failed. Fix them and
  re-run the failing check, or start again:
  1. Delete the folders it printed (`app/modules/<key>`, `tests/modules/<key>`, the frontend's
     `src/modules/<slug>`).
  2. Run `git restore src/core/api/schema.d.ts` in the frontend.
  3. Run `poe openapi` here. `openapi.json` isn't in git (it's made from the code), so this
     writes it again without the module.

### Each side alone

It needs Node 24 on `PATH`, for the frontend. Each side's generator also runs alone:
1. `poe new-module --spec <file> --backend-only` here.
2. `poe openapi` here, then `npm run gen:api` in `tech-toppings-frontend-v1`, so the new routes
   are typed.
3. `npm run new-module -- --spec <file.json>` there (`--check` only checks the spec, not
   whether the module is there already).
   `scripts/new-module.ts` runs on Node with no build step.
   - The JSON is the YAML's content as JSON, saved as UTF-8. This makes it from the YAML:

     ```powershell
     .venv\Scripts\python -c "import json, sys, yaml; json.dump(yaml.safe_load(open(sys.argv[1], encoding='utf-8')), open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)" docs\modules\plants.yaml plants.json
     ```

   - A byte-order mark is fine. UTF-16, which Windows PowerShell's `>` writes, is refused with
     a message saying so.
   - This generator checks the key's shape but not the backend's refusals (Python keywords, the
     platform's paths): the full command checks those.

## 3. What it makes

The module keeps one kind of record, an *item* with a short text (1 to 200 characters), as a
starting point to grow from.

**Backend**: the flat module layout (`docs/plan.md` §3.1 and D2), in `app/modules/<key>/`. Two
differences from the plan's list: the repository sits in `models.py` beside its document, and
the tests go in `tests/modules/<key>/`, with the project's other tests.

| File | What it holds |
|---|---|
| `__init__.py` | The module's docstring (its English tagline) |
| `manifest.py` | `MANIFEST`: key, version `0.1.0`, status, order, router, documents, tile stat |
| `models.py` | `<Key>Item(OwnedDocument)`, collection `<key>_items`, and `<Key>ItemRepository(ScopedRepository)` |
| `schemas.py` | `<Key>ItemIn` (`text`), `<Key>ItemOut` (`id`, `text`, `created_at`, `updated_at`): named after the module, so two modules never share an API type |
| `service.py` | `<Key>Items(scope, clock)`: add, list, get, change, delete (soft), count |
| `router.py` | The routes below, under `/v1/<slug>` |
| `stats.py` | The tile figure: how many items (label `items`) |

```
POST   /v1/<slug>/items             {text}  -> 201 ItemOut
GET    /v1/<slug>/items             ?page&page_size&sort  -> Page[ItemOut], newest first
GET    /v1/<slug>/items/{item_id}           -> ItemOut
PATCH  /v1/<slug>/items/{item_id}   {text}  -> ItemOut
DELETE /v1/<slug>/items/{item_id}           -> 204
```

Every route belongs to the signed-in user (`UserScope`): another user's items answer 404. A
person's export and account deletion include them with no extra code (ADR-0017).

**Backend tests** (`tests/modules/<key>/`):
- `test_<key>.py`: the routes round trip; text checks; the tile figure; one user never sees
  another's items.
- `isolation.py`: how the route matrix (`tests/isolation`) makes and checks an item, for
  every route that takes an `item_id` under `/v1/<slug>/`. It may add `ROUTE_BODIES` and
  `ROUTE_QUERIES` for routes whose generic body, or missing query, would fail validation
  before the lookup (Labor's do).

**Frontend**, in `src/modules/<slug>/`:

| File | What it holds |
|---|---|
| `manifest.ts` | The tile: icon, colour, keywords, the spec's English strings (with the tile figure's label), and the page route |
| `strings/{ta,hi}.json` | The same strings in Tamil and Hindi, loaded with the language rather than with the first page (the performance budget) |
| `api.ts` | TanStack Query hooks over the typed client: the list (a page at a time), add, change, delete |
| `items-page.tsx` | The page at `/m/<slug>`: the list with *Show more*, an add form, edit and delete for each item (delete asks first, Cancel focused), an empty state. The focus follows each change for keyboard users |
| `i18n/{en,ta,hi}.json`, `i18n/index.ts` | The page's text: the spec's, plus the generic words (save, edit, delete ...), already translated |
| `items-page.test.tsx` | Adding, changing and deleting through the page against a fake API, where the focus goes, *Show more*, and an axe check |

**Checks that cover it with no change**:
- module discovery (the manifest, its paths);
- the route matrix (isolation);
- the architecture rules (a module imports `app.platform` only);
- the frontend registry;
- the translation check (every text in every language).

The e2e accessibility sweep (`e2e/smoke.spec.ts`) visits a fixed list of pages, so it doesn't
reach a new module's page; the module's own test runs axe on it. Add `/m/<slug>` to that list
(with a stub for its API) once the module is worth sweeping in both themes.

## 4. Afterwards

1. Read the new files, then commit them: the generator commits nothing.
2. Grow the module: more fields in `schemas.py`/`models.py` and the form, search
   (`search.py`), dashboard widgets (`widgets.py`), agent skills (`skills/`), as `expenses`
   does.
3. A module page in `docs/modules/<key>.md` describes it for people.
