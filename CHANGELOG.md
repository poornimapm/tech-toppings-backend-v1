# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Phase 9a: planning pack (2026-10-16)

- **Scheduled jobs** (`app/platform/jobs`, ADR-0007): `@job("daily"|"each")` plugins, caught
  up when the bell polls `GET /v1/notifications`, with a daily lease per user in `job_runs`.
  Hidden, token-protected `POST /v1/internal/jobs/{job}/run` (`JOBS_TOKEN`, `X-Job-Token`)
  and the `.github/workflows/jobs-cron.yml` workflow, a no-op until its secrets are set.
- **Reminders** (`app/platform/reminders`, ADR-0012): `Reminders.schedule` / `cancel` for any
  module, told once per due date at `REMINDERS_SEND_HOUR` local time; `GET /v1/reminders`,
  snooze and dismiss.
- **Budgets** (`/v1/expenses/budgets`): overall, category or tag (trips and events, U7), per
  week, month or chosen dates, with pace and what is left a day; `expenses.budget_near` and
  `expenses.budget_over`, each once per period.
- **Goals** (`/v1/expenses/goals`): funded by hand (contributions, once per Idempotency-Key)
  or by net savings since a day (U1); `expenses.goal_reached`; revisions, delete and undelete.
- **Repeating entries and bills** (`/v1/expenses/recurring`, U3 part 1): rules that add their
  entries in the daily job once per due date, or remind (`expenses.bill_due`) and are paid
  with `mark-paid`; suggestions from the user's own entries to confirm or dismiss.
- **Loans** (`/v1/expenses/ious`, U2): money lent or borrowed, payments once per key, balances
  by person (`/ious/summary`), due reminders (`expenses.iou_due_lent`, `iou_due_borrowed`).
- **Loans by voice**: the `track-expenses` skill reads loans and payments in English, Tamil
  and Hindi into `expenses_iou_draft` drafts (client tool), with `expenses_ious` to find the
  loan; 48 parser cases and 23 near misses that must stay entries.
- **Duplicate warning**: `POST /v1/expenses/entries/duplicates/check`, advice only.
- Numbers in `data/planning.yaml`; settings groups `JOBS_` and `REMINDERS_`.
- Platform: `same_secret` (constant-time token check), `ActorContext.today()`, ledger lines on
  revisioned records (`append_once`), `answer_once` and `patched` helpers.

### Changed

- The expenses module is version 0.3.0.
- CORS exposes `Idempotent-Replayed`, so the app can tell a replayed save.

### Added — Phase 8: overview and insights (2026-10-14)

- **Analytics** (`/v1/expenses/analytics/*`): summary (against the same number of days
  before), by category, by person (U6), daily, monthly, net savings (U1), top products, top
  places, weekday heatmap, running total, month-end forecast and unusual spending. Each takes
  the entries list's filters (ADR-0016); tuning in `data/analytics.yaml`.
- The entries list filters by exact `merchant`.
- **Insights** (`/v1/expenses/insights`, `POST /insights/refresh`): a week's or month's facts
  and a summary by the `explain-spending` skill (AI, or its templates without AI). AI summaries
  are kept while their facts are unchanged, so a second look makes no AI call. Tool
  `expenses_insight_facts`.
- Platform: `run_skill` runs one skill for the caller from module code; `AgentMode` and
  `RunStatus` are part of the platform API.

### Added — Phase 7: products and prices (2026-10-14)

- **Quantity and unit** on entries (`quantity`, `unit`), from a unit table in
  `data/units.yaml` (`GET /v1/expenses/units`). The rule parser reads them ("2 kg", "அரை
  கிலோ", "एक दर्जन") into their own fields instead of the title; drafts carry them.
- **Products** (`/v1/expenses/products`): what the user buys, grouped by name (ADR-0015);
  price per unit, lowest / highest / average / last, change since the purchase before, prices
  by place and the cheapest place. Products start on their own for expenses with a quantity,
  or by `POST /products` ("Track price"); names can be edited, products merged, kept apart or
  no longer followed.
- **Price alerts**: a limit per unit; an expense above it sends `expenses.price_alert` once.
- **`tidy-products` skill**: suggests products and titles that are the same thing (AI, or
  word groups in English, Tamil and Hindi plus spelling); the user confirms each. Tools
  `expenses_products`, `expenses_items`, `expenses_product_matches` (client).
- `StaticRequest.chosen`: a static script can tell that a page asked for its skill by name.
- Parser cases: 174 (four with units), with quantities labelled and scored.

### Added — Phase 6: capture by voice, text and SMS (2026-10-14)

**Capture through the assistant**
- The `track-expenses` skill turns a sentence, a list or a pasted bank SMS into draft entries
  for the user to check (client tool `expenses_draft`); nothing is written by the assistant.
  `expenses_add` (a write proposal) is gone.
- New read tool `expenses_habits`: what the user logs most and its category.
- Runs take `context.input_type` (`text`, `speech`, `sms`) when a page asks on purpose.

**Rule parser** (`app/modules/expenses/parsing`, the skill's static mode)
- English, Tamil, Hindi and mixed text: digits in any script, number words, "2k", lakh and
  crore, Hindi fractions; relative and written dates; quantities; several items; income;
  merchant after "at"; categories from the user's habits, then keywords.
- Bank and UPI messages: amount, debit or credit, date and the other party.
- All vocabulary and formats are YAML in the skill's `references/`.
- 170 labelled inputs (50 English, 40 Tamil, 40 Hindi, 20 mixed, 20 SMS) plus 40 held out;
  `poe eval-parser` reports accuracy by language and field, and CI gates it.

**Tests (432, 98.6% coverage)**
- Parser units, the accuracy gate, capture in static and agent mode, AI failures (429, 5xx,
  timeout, invalid JSON, used-up quota) falling back to the rules or saying so, an invalid
  category returned to the model, an instruction inside an SMS treated as text, and no
  typed or pasted text in the logs.
- The test module gained a write tool and skill, so proposals and the module switch are
  tested on the platform's own fixture.

### Added — Phase 5: expense core and revisions (2026-10-13)

**Reusable history (ADR-0002)**
- `core/revisions.py`: `RevisionedDocument`, `Revision` and `RevisionedRepository`. Every
  create, update, delete, undelete and restore writes exactly one revision (changed fields
  and a snapshot), without transactions; a crash between the record and its revision is
  finished by the next read or write.
- Updates carry the revision that was edited: a stale one answers `409 VERSION_CONFLICT`.
- `create_once` and the `Idempotency-Key` header (`core/idempotency.py`): a retried create
  returns the first record instead of adding another.
- `core/money.py`: exact conversion between minor units and decimal amounts.

**Expenses module (beta)**
- Entries with kind (expense or income), category, calendar date, time, merchant, payment
  mode, for whom, tags, note and link attachments.
- List with filters, search, sorting and paging; totals per currency for the same filters;
  suggestions from what was used before.
- Soft delete and undelete; history per entry; restore any earlier revision.
- System categories from a data file plus the user's own (add, rename, archive).
- Welcome-tile figure, three dashboard widgets, and activity entries for every change.
- `track-expenses` skill and four agent tools; adding is a proposal the user confirms.

**Platform**
- A switched-off module is off for the assistant too: its skills are not offered and its
  pending proposals fail with `MODULE_DISABLED`.
- In static mode a module's own skills answer before the platform's general ones.
- `ScopedRepository`: `build()`, and `include_deleted` on `find`, `get` and `list`.

**Tests (370, 98.4% coverage)**
- One revision per change; restore equals the old snapshot; stale and simultaneous edits;
  two kinds of crash recovery; replay by key (repeated, simultaneous, after a crash, per
  user).
- Filters, sorting, totals, categories, the tile and widgets, the assistant's proposal, and
  the isolation matrix for every expense route.

### Changed

- The module boundary test lets a module import its own package (still nothing else beyond
  `app.platform`).

### Added — Phase 4b: dashboard, activity and notifications (2026-10-13)

**Dashboard (ADR-0014)**
- Widget contract: `@widget("kpi" | "series" | "list")` functions in a package's
  `widgets.py`, discovered like tools and skills. Titles live on the client.
- `GET /v1/dashboard?from&to`: platform widgets plus those of the modules available to the
  user, run in parallel with a timeout. A failed widget is `data: null`, never a zero.
- `GET /v1/admin/dashboard`: the same widgets across all users, aggregates only (no list
  widgets); 403 for non-admins.
- `DateRange` in the user's time zone, with the previous period for trends
  (`422 RANGE_INVALID`).
- First widgets: actions taken, recent activity, assistant requests per day.

**Activity and notifications**
- `ActivityLog` and `GET /v1/activity`: module pin, hide, arrange and notify-me changes are
  recorded as codes and expire by TTL.
- `Inbox` and `/v1/notifications` (list, unread filter, read one, read all, remove), with a
  per-user `dedupe_key` so a notice is never delivered twice.
- Launch notices: a user who asked to hear about a module is notified once, the next time
  their modules load after it becomes available.
- `check-notifications` skill (agent and static mode, English/Tamil/Hindi lexicon) with
  three tools: list, mark all read (a proposal), open the page.

**Platform**
- `ActorContext` replaces `ToolContext`: tools and widgets take the same context.
- `plugins.contributions()` collects decorated functions; `agents/lexicon.py` holds the
  lexicon matching every static script shares; `core/concurrency.settle()` runs optional
  work with a timeout.
- A static script may answer tentatively, so a more specific skill wins.
- Settings: `DASHBOARD_*`, `NOTIFICATIONS_*`.
- `poe typecheck` also checks each skill's `scripts/` on its own (they share file names).

**Tests (326, 98.3% coverage)**
- Two users each see only their own figures, activity and notifications; the isolation
  matrix covers notification ids.
- Admin view: totals across users, no list widgets, 403 for others.
- Date ranges across time zones, widget failure and timeout, launch notices delivered once.

### Added — Phase 4: skill-driven agents (2026-10-09)

**Agent platform (ADR-0013)**
- Agents (`agents/<name>.md`), skills (`skills/<name>/SKILL.md`, Agent Skills format) and
  tools (`@tool` in `tools.py`) are discovered from folders. A malformed one stops startup
  with its path. No prompt text lives in Python.
- Tools: read, write or client. The schema comes from type hints and the docstring.
  Mistakes go back to the model as data.
- Progressive disclosure: the agent sees skill descriptions and loads one with `load_skill`.
- Switches:
  - `AGENT_MODE` (agent or static) and `AGENT_SKILL_MODES` (per skill);
  - `AGENT_FALLBACK`: the static script when AI can't run, or an honest "unavailable" with
    the reason.
- An OpenAI-compatible free-provider chain with cooldown on 429; consent; per-user and
  global daily call budgets; redaction of emails and long numbers; step limit; runs kept 7
  days (TTL).
- Writes become proposals: `POST /v1/agents/proposals/{id}/confirm|reject`, an atomic claim
  so a change can't run twice, and expiry.
- `GET /v1/agents` (availability, mode, reason, quota left), `POST /v1/agents/{name}/runs`
  and `GET /v1/agents/runs/{id}`.
- First skill, `manage-modules`: four small tools over `ModuleService`, and a static script
  driven by an English/Tamil/Hindi lexicon file.
- The `assistant` agent (markdown only).

**Platform**
- `platform/plugins.py`: one folder-discovery helper, now also used by module discovery.

**Tests (294, 98.2% coverage)**
- Every switch combination, the agent loop with a fake provider, provider failover and
  cooldown, quota at the start and mid-run, the step limit, redaction, static mode in three
  languages, proposal confirm/reject/expiry/failure, and catalog validation.
- Isolation matrix: other users get 404 on runs and proposals.

### Added — Phase 3: module system (2026-10-09)

**Modules**
- `ModuleManifest` spec: key, version, status (`live` / `beta` / `coming_soon`), order,
  default on/off, settings model, routers, documents and tile stat, all as import paths.
- Discovery from `MODULES_PACKAGES` with strict checks. A broken module stops startup with a
  clear message.
- Documents are registered and routers mounted at `/v1/<slug>` and `/v1/admin/<slug>` with
  no edit to `create_app`.
- Coming-soon manifests for expenses, medicine, receipts, study, gov_docs, meals, habits and
  journal.

**Catalog API**
- `GET /v1/modules` and `GET /v1/modules/{key}`: manifest, admin overrides and the user's
  preferences, merged.
- Pin, turn on or off for me, arrange (`PUT /v1/modules/order`), and "notify me" for
  upcoming modules.
- Per-user module settings with a JSON Schema endpoint, strict validation that never echoes
  values, and graceful fallback for outdated stored values.
- Tile figures run in parallel with a timeout; a failing module is logged and skipped.

**Enforcement**
- A gate in front of every module route returns `403 MODULE_DISABLED` when the module is off
  globally or for the user. Admin routes stay reachable.
- import-linter contracts: modules are independent and import only `app.platform`. An AST
  test covers the same rule for future sub-packages and the test modules.
- `app.platform` now re-exports the API modules are written against.

**Tests (218, 98.0% coverage)**
- Demo module dropped in as a folder: it is listed, mounted, counted and gated with no
  other edit.
- Discovery failure cases, catalog filters and sorting, pins and order persistence, admin
  overrides, notify-me, settings, and the gate.
- The isolation matrix now covers catalog routes: acting on a shared module key changes only
  the caller's own state.

### Fixed
- Each app built its own TLS context for outbound HTTP, which took about 4 s per app on
  Windows. The context is now built once per process.
- Document classes defined after a first Beanie initialisation inherited `"_id"` as their
  default id; abstract bases are now cleaned after initialisation.

### Added — Phase 2: auth, users, RBAC (2026-10-08)

**Accounts**
- Registration through an email allowlist (`AUTH_REGISTRATION_MODE` = `allowlist` | `open` |
  `closed`). The bootstrap admin email is always allowed.
- Argon2id password hashing (OWASP parameters, rehash on login) and a configurable policy.

**Sign-in**
- Login with lockout and exponential backoff.
- Per-client rate limits on login, register, refresh and password change (`limits`).

**Sessions and tokens**
- 15-minute JWT access tokens with `sub`, `role`, `tv` (token version) and `sid` (session).
  The user is reloaded on every request, so disable, role change, password change and
  logout-all apply immediately.
- One `auth_sessions` document per device. The refresh token is opaque, stored as a SHA-256
  digest, rotated on every use and sent only as an `HttpOnly; SameSite=Strict` cookie.
- Reuse detection revokes the session. A 10-second grace window returns a retryable
  `409 AUTH_REFRESH_RACE` when two tabs refresh at once.
- Sliding 30-day expiry with a 90-day absolute cap.
- CSRF header on cookie-authenticated endpoints.
- Logout, logout-all, and password change (signs out other devices; required after an
  admin reset).

**Google and profile**
- Optional Google sign-in: ID-token verification against Google's keys (cached), linking by
  verified email, allowlist applies.
- `GET/PATCH /v1/me`: name, language, theme, UI mode, currency, time zone, AI consent.
  Protected fields are rejected.
- `GET /v1/me/sessions` (paginated, sortable, current device marked) and
  `DELETE /v1/me/sessions/{id}`.

**Data access and roles**
- `ScopedRepository` + `Scope`: owner filter and soft-delete filter on every query. Ownership
  stamped on create. Other users' records return 404.
- `CurrentUser`, `AdminUser`, `UserScope` and `require_role` dependencies.

**Operator tasks**
- `poe seed` (bootstrap admin, idempotent) and `poe reset-password --email …` (one-time
  password, forced change, signs out everywhere).

**Core building blocks**
- Clock injection, typed `Page[T]` pagination with whitelisted sort fields, and generic
  401/403/409/429 error classes with headers.

**Tests (152, 97.8% coverage)**
- Route-matrix isolation: every id route; user B always gets 404.
- "Auth by default" check over the OpenAPI inventory.
- Admin vs user.
- No secrets in logs.
- Lockout, rotation, reuse, race, expiry and Google flows.

### Fixed
- Registration no longer trims whitespace from passwords, which login would not match.
- Error responses no longer carry `X-Request-ID` twice.

## Phase 1 — foundations (2026-10-08)

**App and config**
- FastAPI app factory `create_app(settings)` with a lifespan, run via
  `uvicorn --factory app.main:create_app`.
- Typed settings in prefixed groups (`APP_`, `MONGO_`, `LOG_`, `CORS_`, `SECURITY_`), loaded
  from the environment, `.env` and `environments/<APP_ENV>.env`.

**Logging, errors and middleware**
- structlog logging: JSON or console output to stdout, request IDs, redaction of sensitive
  keys, and stdlib/uvicorn logs routed through the same pipeline.
- Error envelope `{code, message, details, request_id}` for app errors, validation errors
  (submitted values are never echoed), HTTP errors and unhandled exceptions.
- Middleware:
  - request context: `X-Request-ID`, plus an access log;
  - security headers, with optional HSTS;
  - a CORS allowlist.

**Database and health**
- MongoDB through PyMongo Async + Beanie 2.
- Beanie is initialized lazily, so the app starts even while MongoDB is down and recovers
  automatically.
- `/healthz` (liveness) and `/readyz` (readiness: 503 + envelope when MongoDB is down).

**Tooling and CI**
- Project-level `.venv` with dependencies locked by pip-tools (`requirements/*.lock`).
- poethepoet tasks: `dev`, `test`, `lint`, `fmt`, `audit`, `check`, `lock`, `sync`, `openapi`.
- ruff, mypy `--strict`, import-linter layer contract.
- Local-only pre-commit hooks, which also block `.env` files.
- pytest suite against a real MongoDB throwaway database: 39 tests, 98.9% coverage.

**Docker**
- Multi-stage non-root Dockerfile with a health check.
- docker-compose with MongoDB 8.0.

**CI and docs**
- GitHub Actions: lint, typecheck, test (mongo service), pip-audit, actionlint, gitleaks,
  Docker build.
- Docs: plan, ADRs 0001–0012, architecture overview, reference analysis, free-tier limits.
