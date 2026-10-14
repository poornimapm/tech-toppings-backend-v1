# Architecture

This page summarizes how the system is built today. The full design and its rationale are in
[plan.md](plan.md), and individual decisions are in [adr/](adr/).

## System

```mermaid
flowchart LR
  Browser["React PWA"] -- "/api/*" --> Proxy["Cloudflare Pages Function<br/>(prod) / Vite proxy (dev)"]
  Proxy --> API["FastAPI (Render free)"]
  API --> DB[("MongoDB<br/>local 8.0 / Atlas M0")]
```

The browser reaches the API **same-origin** through `/api`:
- in development, through the Vite dev-server proxy;
- in production, through a Cloudflare Pages Function.

This keeps the refresh-token cookie first-party and makes CORS unnecessary (ADR-0003, ADR-0006).

## Backend layers

| Layer | Package | May import |
|---|---|---|
| Core | `app.core` | stdlib, third-party |
| Platform | `app.platform` | core |
| Modules | `app.modules.<key>` | `app.platform` only (the names it re-exports) |

`app.main` composes everything. import-linter (`pyproject.toml`) fails CI if:
- a lower layer imports a higher one;
- one module imports another;
- a module imports `app.core` or a platform sub-package instead of `app.platform`.

`tests/architecture/test_module_boundaries.py` checks the same import rule with an AST scan,
so it also covers platform sub-packages added later and the test-only modules.

## Request lifecycle

1. **`RequestContextMiddleware`** (outermost):
   - takes a valid incoming `X-Request-ID` or generates one;
   - stores it in a contextvar and in `request.state`;
   - binds it to every log line;
   - writes one `http_request` access-log event (health probes are logged at DEBUG).
2. **`SecurityHeadersMiddleware`**:
   - `nosniff`, `DENY` framing, `no-referrer`, `no-store`;
   - HSTS when `SECURITY_HSTS_MAX_AGE_SECONDS > 0`.
3. **`CORSMiddleware`**: only installed when `CORS_ALLOWED_ORIGINS` is set.
4. **Router → service → repository.** Services raise `AppError` subclasses.
5. **Exception handlers** render every failure as
   `{code, message, details, request_id}`. Unhandled errors are logged with their stack trace
   and returned as a generic `INTERNAL_ERROR`, so internals never leak.

## Database lifecycle

`Database` (in `app.core.db`) owns a single `AsyncMongoClient`.

**Startup.** `check_ready()` pings the server and runs `init_beanie` once. If MongoDB is
unreachable, the app still starts: `/readyz` returns 503, and the next successful check
initializes Beanie, so no restart is needed. This matters for Atlas M0 maintenance and
Render cold starts.

**Shutdown.** The lifespan closes the client.

## Authentication and sessions

```mermaid
sequenceDiagram
  participant B as Browser (SPA)
  participant A as API
  B->>A: POST /v1/auth/login (email, password)
  A-->>B: {access_token (15 min), user} + Set-Cookie tt_refresh (HttpOnly, path /api/v1/auth)
  B->>A: GET /v1/me  Authorization: Bearer access_token
  A-->>B: 401 AUTH_TOKEN_EXPIRED (after 15 min)
  B->>A: POST /v1/auth/refresh  (cookie + X-TT-CSRF)
  A-->>B: new access_token + rotated cookie
  B->>A: replay GET /v1/me with the new token
```

**Access token.** A JWT signed with `AUTH_JWT_SECRET`. Claims:

| Claim | Meaning |
|---|---|
| `sub` | user id |
| `role` | user role |
| `tv` | token version |
| `sid` | session id |
| `exp` | expiry: 15 minutes |

`authenticated_user` reloads the user on every request and checks `disabled_at`, `deleted_at`
and `tv`. Bumping `token_version` (password change, logout-all, admin reset) therefore
invalidates every outstanding access token at once.

**Refresh sessions** (`auth_sessions`, one per device):
- The record stores only SHA-256 digests.
- Each refresh swaps in a new token and keeps the previous digest.
- A replaced token coming back revokes the session. Within 10 s it is a two-tab race instead
  (409, retry).
- Expiry slides 30 days per use and is capped at 90 days.
- A TTL index purges records a week after the session ends.

**Dependencies:**

| Dependency | Who passes |
|---|---|
| `CurrentUser` | Signed in, no pending password change (the default) |
| `CurrentUserAllowingPasswordChange` | Also accounts with a pending change. Only `GET /v1/me`, password change and logout-all use it |
| `AdminUser` | Admin role |
| `UserScope` | Provides the caller's data `Scope` |

**Lockout and rate limits.**
- Failed logins count per account. At `AUTH_LOCKOUT_THRESHOLD` the account locks for 60 s,
  doubling with each further failure up to 1 h.
- Auth endpoints are also rate-limited per client IP.

## Data access and isolation

User-owned documents extend `OwnedDocument`: `user_id`, `module_key`, `created_by` /
`updated_by`, timestamps and `deleted_at`.

They are read and written only through `ScopedRepository`, which:
- ANDs `{user_id: scope.owner}` and `{deleted_at: null}` into every query;
- stamps ownership on create, whatever the caller passed;
- answers 404 for records that are missing, malformed or owned by someone else.

`Scope.unrestricted()` (admin, no owner filter) is allowed only under `app/platform/admin/`.

Three test suites enforce these rules:
- `tests/isolation/test_route_matrix.py`: user B gets 404 on user A's resource for **every**
  `/v1` route with an id. New routes must register a resource factory there.
- `tests/architecture/test_access_rules.py`: every non-public `/v1` route returns 401 to an
  anonymous caller, and the unrestricted-scope rule holds.
- Both suites read the route inventory from the OpenAPI schema. FastAPI keeps included
  routers nested, so `app.routes` alone would miss routes.

## Revisions and idempotent creates

`core/revisions.py` gives any module append-only history without transactions (ADR-0002).
A module declares a `RevisionedDocument`, a `Revision` collection and a
`RevisionedRepository`; every write through that repository also writes one revision.

```mermaid
sequenceDiagram
  participant R as RevisionedRepository
  participant D as record
  participant H as revisions (append-only)
  R->>D: 1. conditional update: new fields, revision_no n+1, pending_revision
  R->>H: 2. insert revision n+1 (unique index: repeating is harmless)
  R->>D: 3. clear pending_revision
  Note over R,H: a crash after 1 is finished by the next read or write of that record
```

- A revision holds the action (`create`, `update`, `delete`, `undelete`, `restore`), who and
  how (`source`), the changed fields (`old` and `new`) and a snapshot of the record's fields.
- The record's own fields must be JSON values, so a snapshot is exactly what is stored and
  `restore` can put it back as it is. Restoring writes a new revision; history is never
  rewritten.
- An update carries the revision the caller edited (`expected`). If the record has moved on,
  the answer is `409 VERSION_CONFLICT` with the current revision; two edits at once cannot
  both win, because step 1 only matches the revision it read.
- A change that changes nothing writes no revision and no new version.
- `create_once(fields, client_key)` creates at most one record per user and key (a unique
  partial index). The key is the request's `Idempotency-Key` header (ADR-0010): a retried or
  replayed create returns the first record, with `Idempotent-Replayed: true`.

## Expenses

The first real module (`app/modules/expenses`, in beta). It uses only `app.platform`.

```
manifest.py   router.py   domain.py    stats.py (tile)   widgets.py   tools.py   units.py
data/categories.yaml                  system categories: key, emoji, accent
data/units.yaml                       units by dimension, with their factor
features/categories/                  the user's own categories
features/entries/                     entries, their history, filters, totals
features/products/                    products (names), prices per unit, alerts
skills/track-expenses/                SKILL.md, lexicon, static script (the rule parser)
skills/tidy-products/                 SKILL.md, word groups, static script
```

- **Money** is `amount_minor` (whole minor units) plus `currency` (ADR-0004); `core/money.py`
  is the only place that converts. Entries are kept in the user's base currency for now:
  another currency is `422 EXPENSE_CURRENCY_UNSUPPORTED` until exchange rates arrive.
- **Dates** are calendar days (`YYYY-MM-DD` text), never instants, so "yesterday" cannot move
  with a time zone.
- **Categories:** system ones are data, not records (their `key` is their id and the client's
  translation key); a user adds their own. Archiving keeps the record, so old entries keep
  the name, and frees the name. A new or re-filed entry needs a live category of its kind
  (`422 EXPENSE_CATEGORY_INVALID`).
- **Entries API** (`/v1/expenses/entries`): `GET` (filters `kind`, `from`, `to`,
  `category_id`, `tag`, `for_whom`, `payment_mode`, `q`, `min_amount`, `max_amount`,
  `deleted`; sort by `date`, `amount_minor`, `created_at`, `title`), `POST` (once per
  `Idempotency-Key`), `GET /summary` (totals per currency for the same filters),
  `GET /facets` (tags, people and merchants used before), `GET/PUT/DELETE /{id}`,
  `POST /{id}/undelete`, `GET /{id}/revisions`, `POST /{id}/restore`.
- **Quantity** is `quantity` + `unit` (a code of `data/units.yaml`, `GET /v1/expenses/units`),
  both or neither.
- **Products** (`/v1/expenses/products`, ADR-0015): a product is a set of names; an expense
  belongs to the product that has its title, worked out when read, so merging or learning a
  name never rewrites entries. `GET` (with `title` for the one that claims it), `POST` (follow
  a title), `GET/PATCH/DELETE /{id}` (prices per unit, places and cheapest; names, price unit,
  alert; stop), `POST /{id}/merge`, `POST /{id}/apart`. Saving an expense with a quantity
  starts a product for a new name and checks the price alert (one notification per entry).
- **Platform services:** each change is noted in the activity feed; the Welcome tile shows
  this month's spending; the dashboard gets spent, received and spending per day; a price
  over a product's alert goes to the notification inbox.
- **Capture and the assistant:** the `track-expenses` skill reads what the user says, types
  or pastes into drafts (the client tool `expenses_draft`); the user checks them on a confirm
  card and saves them through the API. Its tools: categories, habits (the user's usual
  category per title), list, draft, open; none of them writes. Without AI, its static script
  is the rule parser (`parsing/`, vocabulary in the skill's `references/`). The
  `tidy-products` skill suggests products and titles that are the same thing (client tool
  `expenses_product_matches`) for the user to combine. See
  [modules/expenses.md](modules/expenses.md) for the flows and the accuracy report.

A switched-off module is off everywhere: its routes answer `403 MODULE_DISABLED`, its widgets
leave the dashboard, its skills are not offered to the assistant, and a proposal made
earlier fails with `MODULE_DISABLED` when confirmed (`ModuleService.usable_owners`).

## Modules

A module is a folder under a package listed in `MODULES_PACKAGES` (default `app.modules`;
tests add `tests.fixtures.modules`) with a `manifest.py`:

```python
MANIFEST = ModuleManifest(
    key="expenses", version="0.1.0", status=ModuleStatus.LIVE, order=10,
    settings_model=ExpenseSettings,                   # extra="forbid", validated per user
    router="app.modules.expenses.router:router",      # mounted at /v1/expenses
    admin_router="app.modules.expenses.admin:router", # mounted at /v1/admin/expenses
    documents=("app.modules.expenses.models:Expense",),
    tile_stat="app.modules.expenses.stats:month_total",  # async (scope, clock) -> TileStat
)
```

**Discovery** (`platform/modules/discovery.py`) runs in `create_app`, before the database
starts. It imports each manifest and checks that:
- the key equals the folder name and is unique across packages;
- every import path stays inside the module's own package and resolves to the right kind of
  object (router, Beanie document, async function);
- a settings model forbids unknown fields, and a coming-soon module has no routes.

Any failure stops startup with a message naming the module. The registry then registers the
documents with Beanie and mounts the routers. Name, icon and colour are not here: they live
in the module's frontend folder, so the backend describes behaviour only.

**What a user sees** (`GET /v1/modules`) is three layers merged, later ones winning:

| Layer | Stored in | Controls |
|---|---|---|
| Manifest | code | status, default order, enabled by default |
| Admin override | `modules` (written by the admin console, Phase 10) | status, global on/off, order |
| User preference | `user_modules`, one per user and module | on/off for me, pinned, position, settings, notify-me |

A module is **available** when it is globally enabled, enabled for the user and not coming
soon. Available modules also get their `tile_stat` figure. Stats run in parallel with a
timeout (`MODULES_TILE_STAT_TIMEOUT_SECONDS`), and a failing stat is logged and skipped, so
one broken module never breaks the Welcome page.

**Availability is enforced on the server.** Every module route sits behind a gate dependency
(signed in, then available) that answers `403 MODULE_DISABLED` before the module's code runs.
Admin routes skip the gate, so admins can still manage a module's data while it is off.

**Catalog endpoints** (`platform/modules/router.py`):
- `GET /v1/modules` (filters `status`, `pinned`; sort `position` or `key`) and
  `GET /v1/modules/{key}`;
- `PATCH /v1/modules/{key}/preferences` (pin, on/off) and `PUT /v1/modules/order`;
- `POST/DELETE /v1/modules/{key}/interest` ("notify me", coming-soon modules only);
- `GET /v1/modules/{key}/settings/schema`, and `GET/PUT /v1/modules/{key}/settings`.

Module keys are a shared catalog, not owned records: every call changes only the caller's own
`user_modules` record. Preference writes are single atomic upserts, so two tabs can't create
duplicates. Stored settings that a newer module version no longer accepts fall back to their
defaults instead of failing.

## Agents

Skill-driven agents (ADR-0013) live in `platform/agents/`. The package only runs them:
- agents are `agents/<name>.md`;
- skills are `skills/<name>/SKILL.md` (Agent Skills format) with an optional
  `scripts/static.py` and `references/`;
- tools are `@tool` functions in a `tools.py`.

All three are discovered from platform sub-packages and modules with the same folder scan
as modules.

```mermaid
flowchart LR
  Ask["POST /v1/agents/{name}/runs"] --> Plan{"policy.plan<br/>mode + blockers + fallback"}
  Plan -- agent --> Loop["model + load_skill + tools<br/>(free providers, quota, redaction)"]
  Plan -- static --> Script["skills/*/scripts/static.py<br/>(same tools, lexicon data)"]
  Plan -- unavailable --> Honest["status: unavailable + reason"]
  Loop --> Run[("agent_runs (TTL)")]
  Script --> Run
  Run -- "write tools" --> Proposal["proposals"] -- "POST .../confirm" --> Tool["tool runs with the user's Scope"]
```

| Tool effect | During a run | Afterwards |
|---|---|---|
| `read` | runs at once, result goes back to the model | — |
| `write` | becomes a proposal with a summary | runs only when the user confirms (atomic claim, expires) |
| `client` | validated, handed to the UI (open a page) | the UI acts |

`AGENT_MODE`, `AGENT_SKILL_MODES` and `AGENT_FALLBACK` decide between agent mode, static
mode and an honest "unavailable". The blockers are: no consent, quota used up, no
provider, all providers failing.

Daily budgets are counted per AI call (`agent_usage`, per user and global, by UTC day).

## Dashboard, activity and notifications

Three platform services every module shares (ADR-0014). Tools and widgets both receive an
`ActorContext` (user, scope, clock, settings, app state), so they read through the same
`Scope` as the API.

**Widgets.** A package contributes dashboard figures with a `widgets.py`:

```python
@widget("kpi")
async def expenses_total(ctx: ActorContext, period: DateRange) -> Kpi:
    return Kpi(value=..., previous=..., format="currency", currency=ctx.user.base_currency)
```

| Kind | Shape | Drawn as |
|---|---|---|
| `kpi` | a value and the previous period's | a figure with its trend |
| `series` | one value per day | an area chart, also readable as a table |
| `list` | rows (`title`, or `code` + `params`) | a short feed |

- `GET /v1/dashboard?from&to` runs the platform's widgets and those of the modules available
  to the user, in parallel. Each has `DASHBOARD_WIDGET_TIMEOUT_SECONDS`; a failed or slow
  one is logged and sent as `data: null`, never as a zero.
- `GET /v1/admin/dashboard` runs the same widgets with `Scope.unrestricted` and leaves out
  `list` widgets: admins see totals, never anyone's rows.
- `DateRange` (`core/daterange.py`) holds calendar days in the user's time zone. Defaults
  and the longest range are `DASHBOARD_DEFAULT_RANGE_DAYS` / `DASHBOARD_MAX_RANGE_DAYS`;
  a bad range is `422 RANGE_INVALID`.
- `count_in` and `daily_counts` (zero-filled, time-zone aware) cover the common widgets.

**Activity.** `ActivityLog(...).record("module.pinned", module="journal")` stores a code and
params; `GET /v1/activity` lists the caller's own, newest first. Entries expire after
`DASHBOARD_ACTIVITY_RETENTION_DAYS`.

**Notifications.** `Inbox(...).notify("module.launched", module=key, dedupe_key=...)`. A
`dedupe_key` is unique per user, so a repeated trigger never delivers twice.

- `GET /v1/notifications?unread=`, `POST /v1/notifications/read-all`,
  `POST /v1/notifications/{id}/read`, `DELETE /v1/notifications/{id}`.
- Notifications expire after `NOTIFICATIONS_RETENTION_DAYS`.
- A launch notice is delivered the next time the user loads `GET /v1/modules`, which needs
  no scheduler (ADR-0007).
- The `check-notifications` skill gives the assistant three tools: list (read), mark all
  read (write, confirmed by the user) and open the page (client).

In both feeds `params.module` is a module key; the client shows the module's name.

## Configuration

`app.core.config` is the only module that reads the environment.

Each concern is a frozen `BaseSettings` group with its own prefix:

| Prefix | Group |
|---|---|
| `APP_` | `AppSettings` |
| `MONGO_` | `MongoSettings` |
| `LOG_` | `LogSettings` |
| `CORS_` | `CorsSettings` |
| `SECURITY_` | `SecuritySettings` |
| `AUTH_` | `AuthSettings` (tokens, cookie, registration, password policy, lockout, Google) |
| `RATE_LIMIT_` | `RateLimitSettings` |
| `API_` | `ApiSettings` (page sizes) |
| `DEFAULT_` | `DefaultsSettings` (new-account locale, currency, time zone) |
| `MODULES_` | `ModulesSettings` (packages to discover, tile-stat timeout) |
| `AGENT_` | `AgentSettings` (mode, fallback, providers, budgets, retention, redaction) |
| `DASHBOARD_` | `DashboardSettings` (range defaults and limit, widget timeout, activity retention) |
| `NOTIFICATIONS_` | `NotificationSettings` (retention) |
| `BOOTSTRAP_ADMIN_` | `BootstrapSettings` (`poe seed`) |

Sources, from lowest to highest priority:
1. `.env`
2. `environments/<APP_ENV>.env`
3. real environment variables

`create_app(settings)` stores the `Settings` object on `app.state`. Dependencies read it from
there, so each test gets an isolated app.

## Observability

- Logs are JSON on stdout, which Render collects.
- Each line carries `timestamp` (UTC), `level`, `logger`, `event`, plus `request_id` during
  requests.
- Sensitive keys are masked by the `Redactor` processor. The default list is in
  `LOG_REDACT_KEYS`.

## Testing

Tests run against a **real MongoDB**: `TEST_MONGO_URI` (default `localhost:27017`), using a
throwaway database `tt-test-<random>` that is dropped after the session.

They are hermetic: settings variables from the shell or `.env` are stripped. A test-only
module, `tests/fixtures/modules/notes`, goes through the same discovery as real modules and
backs the isolation, RBAC and gate tests.

`Database` removes the query attributes Beanie writes onto abstract base documents (such as
`OwnedDocument`) after initialisation. Without this, a document class defined later in the
same process would inherit `"_id"` as its default id. Apps are built
with explicit `Settings` through `tests/helpers.build_settings` and exercised in-process
(`asgi-lifespan` + `httpx.ASGITransport`).
