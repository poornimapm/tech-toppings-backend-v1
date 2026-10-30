# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Phase 17: Labor's extras

- **Marks kept offline never overwrite newer ones** (ADR-0024): `MarkIn.expected`, the mark the
  sender saw; a mark whose day was marked otherwise since is refused before anything is written
  (409 `LABOR_MARK_CHANGED`, with the mark it has now).
- **Report files**: `GET /v1/labor/projects/{id}/report.csv` and the admin's copy: the report's
  figures, then each day marked and each payment not voided; too many rows are refused before
  the file is made (`LABOR_EXPORT_TOO_LARGE`, `reports.export_max_rows`).
- **Marks by voice**: the `mark-attendance` skill (static rules in English, Tamil and Hindi in
  `references/attendance.yaml`, or the AI with consent) reads "Ravi full day, Meena half day,
  the rest absent" into a draft: tools `labor_day` (read) and `labor_attendance_draft` (client);
  `POST /v1/labor/projects/{id}/attendance/read?day=` runs it for the attendance page. It never
  saves.
- Reports name each worker (`code`, `name`) with what they earned and were paid to date
  (`earned_to_date_minor`, `paid_to_date_minor`); audit entries carry `labels` naming the workers
  they concern (a change's worker, the worker an entry is about, a payment's worker) and their
  project's `currency`.
- `mark-attendance`'s rules reader: one pattern for every name (a long text for 500 workers reads
  in well under a second), a worker sharing a name or number with another left to choose, names
  with initials, "came, half day", comma parts after a leading mark, and the common spellings
  of "didn't come"; from the assistant it answers only for a worker matched for certain.
- The CSV rules (byte-order mark, CRLF, formula guard) are the platform's (`csv_text`,
  `csv_writer`, `safe_text`, `attachment`, `CSV_RESPONSE`, `CSV_MEDIA_TYPE`), shared by expenses
  and Labor; `AgentRun` is in the platform API.

### Fixed — Phase 17

- What a worker is owed counted days recorded before the project's start date, but what they
  earned didn't: both now count every day up to the report's end.
- `GET /v1/agents` lists only the skills of modules the account may use, as a run serves only
  those: a module given to some accounts isn't listed for the others.

### Added — Phase 16: Labor Management

- **Labor Management** (beta, ADR-0023), made by `poe new-module` and grown, for the owners the
  admin makes: projects (open until closed), workers (a number of their own, a photo, an ID
  picture if kept, made inactive never deleted), each project's usual daily wage (one for all or
  by gender) and each worker's own with its history, attendance by day (full 1, half 0.5, absent
  0, one mark per worker and day, each at the wage it was recorded with), payments (partial,
  kept once per `Idempotency-Key`, voided with a reason), reports by day, week or month with
  what is owed, each worker's days and payments between two days, an overview of today, and
  an append-only audit trail with each field from and to. `data/labor.yaml` holds its rules.
- Admin oversight, read only, under `/v1/admin/labor`: owners, projects, workers, reports, audit.
- **Modules only some accounts are given**: a manifest's `audience="granted"`; such a module
  isn't there for anyone not given it (404 `MODULE_NOT_FOUND`); admins see every module.
- **The admin makes accounts**: `POST /v1/admin/users` (a one-time password, shown once, and the
  modules to give); `PUT /v1/admin/users/{id}/modules/{key}` gives or takes one back; accounts
  list their given modules. Audited.
- **Private files** (`app/platform/files`): a small file kept for its owner (type from its
  bytes, size checked), served only through a module's own checks (`private, no-store`).
- `ScopedRepository.first(filters)`; `parse_object_id`, `current_request_id` and `granted_users`
  in the platform API; the admin's module list says each module's `audience`.

### Changed — Phase 16

- The route matrix gives its accounts every module only some accounts may use, and a module's
  `isolation.py` may add the bodies and queries its routes need.

### Added — Phase 15: Receipts & warranty

- **The receipts module** (beta, ADR-0022), made by `poe new-module` and grown: receipts with
  what was bought, the shop, the day (never after today), the amount, the bill's and the thing's
  numbers, a note, and the warranty's last day (`/v1/receipts/items`, `/v1/receipts/options`).
  Where each warranty stands today (none, active, ending, ended) is worked out per request and
  filters lists (`?warranty=`); `?q=` finds words in a receipt.
- A reminder before each warranty ends (`receipts.warranty_ends`), moved or cancelled with the
  receipt; none for a warranty already over.
- Search (the command palette), a dashboard widget (warranties ending soon), the Welcome tile's
  count, and the activity feed.
- **`scan-bill`** skill: a bill's photo (AI) or its text (rules) fills in a receipt through the
  `receipts_draft` client tool; its static reader takes the dearest item, the total, the bill's
  and serial numbers and a printed warranty (`references/bill.yaml`, en/ta/hi).
- `data/warranty.yaml`: the reminder's default and most days, the longest warranty, the lengths
  the form offers.

### Changed — Phase 15

- **The slip reader is the platform's** (`app/platform/slips`: `read_slip`, `word_pattern`,
  `words.yaml`), so any module reads printed paper; the caller says how old a date and how large
  a figure may be. Expenses' receipt reader keeps only what expenses makes of a slip, and reads
  the 20 samples exactly as before. `western_digits` and `on_or_before` moved to the platform.
- Tests that used `receipts` as their upcoming module use `medicine`.

### Fixed — Phase 15

- A shop's name read with a stray opening bracket at its end ("Sri Murugan Stores (") loses it.
- The body-limit tests set a photo limit that fits their small body limit (the startup check
  added in Phase 14 refused their settings).

### Added — Phase 14: receipt scan preview

- **Photo runs** (ADR-0021): `POST /v1/agents/{name}/photo-runs?skill=...` takes a photo as the
  request body (`AGENT_MAX_PHOTO_BYTES`, `AGENT_PHOTO_TYPES`, its type checked against its
  bytes). It is held in memory for the run and sent only to providers marked `"vision": true`;
  the run keeps a count (`photos`), never the photo. Without a vision provider, or AI, the
  switches decide as for any run.
- **`scan-receipt` skill** (expenses): one draft per item line, the shop and day on each, adding
  up to the amount paid. Its static script is a **receipt reader** (`parsing/receipt.py`, words
  in `references/receipt.yaml`) for text read on the phone (`input_type: "ocr"`, up to
  `AGENT_MAX_OCR_CHARS`). `expenses_draft` takes up to 40 drafts.
- **`poe eval-receipts`**: accuracy on 20 labelled sample receipts in English, Tamil and Hindi
  (`tests/modules/expenses/receipts`), on the phone's OCR text, the printed text (`--paper`),
  or by a vision provider on the photos (`--ai`). CI gates the rules at the measured floors.
  Results in `docs/modules/expenses.md`.
- Tests that no photo is kept anywhere: every collection and the logs are searched after a run.

### Fixed (Phase 14 review: 37 findings, all fixed)

- A scan whose AI run stopped before drafting anything (quota, a 429 mid-run) now falls to
  the switches, so the photo is read on the device; a run's answer cut off inside a tool call
  moves on to the next provider; a resting vision provider costs no quota.
- A switched-off module's skill never runs (403), photo or text; only a skill that reads
  photos takes one (422); the longer OCR input limit is for a skill that reads `ocr` only.
- `GET /v1/agents` says per skill where a photo would be read (`photo_mode`).
- `AGENT_MAX_PHOTO_BYTES` must fit in `API_MAX_BODY_BYTES` (checked at startup).
- The receipt reader: no more than the drafts tool takes (a long receipt crashed the scan),
  no phantom drafts from postal codes, bill numbers or date lines, payment lines never the
  total, "amt" totals, "Subtotal" as one word, January by name, counts never taken from a
  price (and weights read as kg), Tamil and Hindi words matched at a word's start (कर is not
  करी), short Indic words counted as words (घी, டீ), column labels cut from item names,
  tidier shop names, a total-only fuel slip filed as fuel.
- The scorer no longer credits a subtotal as a balancing draft or a bare "Store" as the shop,
  and CI gates draft precision too; `--ai` checks tool calls as the app does.
- Sample r09's tax lines now add up to its total.

### Changed

- `ProviderSettings.vision` (default false); `ProviderChain.configured()` is a method that
  can ask for a provider that reads images; skills declare `reads` and `max-output-tokens` in
  their SKILL.md metadata.
- The rule parser's `category()` is shared (a receipt's kind of shop is its last guess before
  "other"), as is the draft-to-arguments helper.

### Added — Phase 13: generator + future scaffolds

- **`poe new-module --spec <file>`** (`scripts/new_module.py`, ADR-0020) makes a working module in
  both repositories from one YAML spec ([`docs/modules/new-module.md`](docs/modules/new-module.md),
  example in `docs/modules/example-module.yaml`).
  - **Spec checks**, all before anything is written: the key (2-24 characters, parts joined by
    single `_`, not a Python keyword, a platform path or a module already there), the icon and
    colour (by the frontend's generator, `--check`), status, order, and texts in en/ta/hi,
    short enough, tidied to one line, and nothing left in English.
  - **Backend**: the module (`app/modules/<key>`: an item with a short text, routes under
    `/v1/<slug>/items`, a tile figure) and its tests (`tests/modules/<key>`).
  - **Frontend**: the module next door, plus regenerated API types.
  - **Checks**: the new module's tests and the shared checks run on both sides.
  - **Safety**: it never writes over a folder; a run that fails or is stopped (Ctrl+C) while
    writing is undone, on both sides. Files that fail the final checks are kept, to look into.
  - Options: `--frontend`, `--backend-only` (the frontend's check still runs when the frontend
    is there), `--no-check`.
  - The English name and tagline go into the module's docstring as ASCII (curly quotes and
    dashes as plain ones), so the project's lint takes any text.
- **`docs/roadmap.md`**: what's next, a first slice for each of the seven upcoming modules, how
  to build one, and the open platform items.
- **`docs/mobile.md`**: the Play Store path (a Trusted Web Activity with Bubblewrap and Digital
  Asset Links) and the iPhone path (a Home Screen web app), with what each needs and costs.

### Changed — Phase 13

- The route matrix also takes a module's own factories (`tests/modules/<key>/isolation.py`), by
  its route prefix.
- The catalog and discovery tests read the module list from discovery, so a new module needs no
  edit there.
- Ruff names the project's packages (`known-first-party`), so a file about to be written sorts
  like one already there.

### Added — Phase 12: deployment + hardening

- **Runbook** `docs/deployment.md` (ADR-0019): the accounts to create, the secrets to make, the
  first deploy, the first admin, the GitHub secrets, the live checks, and day-to-day running
  (backups, monitoring, limits, rotating secrets, troubleshooting).
- **Render Blueprint** `render.yaml`: one free Docker service in Singapore, health check
  `/healthz`, deploys `main` after CI passes, production settings set and every secret asked
  for (none in git). A test starts it in production; another keeps `.env.example` complete.
- **Origin lock**: with `SECURITY_PROXY_SECRET` set, the API answers only calls that carry it
  (the app's `/api` proxy sends it), but `SECURITY_PROXY_EXEMPT_PATHS` (`/healthz`). The client
  address the proxy passes on can then be trusted.
- **Production guard**: with `APP_ENV=production` the server refuses to start without HSTS, the
  proxy secret, a `Secure` cookie, docs off, JSON logs, rate limits on and https-only CORS, and
  names every problem.
- **Compression**: answers of `API_GZIP_MIN_BYTES` (1000) or more are gzipped, never sign-in
  answers (`API_GZIP_SKIP_PATHS`, BREACH). Render's free plan now counts 5 GB out a month.
- **Backups**: `poe backup` and `poe restore` (`BACKUP_*`).
  - Every collection is saved as canonical Extended JSON, gzipped and Fernet-encrypted (always,
    for a database on another machine), with a manifest of source, counts and indexes.
  - A backup is written whole or not at all. Only this database's oldest complete backups are
    let go.
  - Restoring checks every file before writing, writes into empty collections (or `--drop`),
    checks each count before the indexes, and says how far it got if it stops.
  - It asks for the target `host/database` first.
  - `environments/production.env.example` sets these commands up for the live database, which
    every maintenance command names before it starts.
- **Live checks**: `poe smoke-live --app … [--api …]`, and the `smoke-live` workflow (daily and
  by hand; its failure email is the monitoring). It checks the page's headers, a deep link, the
  install files, caching, the API awake and its database, strangers refused, the CSRF rule,
  docs hidden, the origin lock, and, with a test account, sign-in and sign-out.

### Changed — Phase 12

- `docs/free-tier-limits.md` re-checked (2026-10-28):
  - Render's free bandwidth is now 5 GB a month;
  - Atlas's M0 is now the "Free cluster", offered in AWS Singapore;
  - Frankfurter's v2 is current (v1 still works);
  - Gemini and Groq free models changed.
- The jobs cron calls the API through the app's `/api` (`JOBS_API_URL`).
- Only `poe dev` and `poe test` load `.env` into the environment, so `$env:APP_ENV` reaches
  `environments/<APP_ENV>.env` for seed, backup, restore and password resets.
- Tests clear every settings group's variables, whatever groups are added later.

### Added — Phase 11: PWA, i18n, a11y, polish

- **Web Push** (`app/platform/push`, ADR-0018), with no new library:
  - RFC 8291 message encryption and RFC 8292 VAPID, on `cryptography` and PyJWT, checked
    against the RFC's worked example;
  - `/v1/push`: whether pushes are on, subscribe or unsubscribe this browser, send myself a
    test;
  - due reminders are pushed by their own job, `reminders_push`, after delivery in the same
    catch-up, so the inbox never waits on a push service. Each browser has its own deadline
    (`PUSH_DEVICE_TIMEOUT_SECONDS`) inside the round's (`PUSH_TIMEOUT_SECONDS`, which must fit
    the job budget), and at most `PUSH_MAX_PARALLEL` pushes are under way at once. A reminder tried on every browser is done; one cut short goes again after
    the lease, at most `PUSH_MAX_TRIES` times, under the same notification tag. A browser the
    push service says is gone is forgotten, and one that fails never stops the others;
  - a subscription is tied to the signed-in device (the session) and to the current VAPID key:
    signing out, signing the device out, signing out everywhere, a password change, an expired
    session or new keys stop its pushes and delete it;
  - only known push services are sent to (`PUSH_ALLOWED_HOSTS`, normalised), since an endpoint
    is a URL a user supplies; a malformed endpoint, a user name or a port other than 443 gets 422;
  - startup checks the VAPID pair (the public key must be the private key's), the allowed hosts
    and `RATE_LIMIT_PUSH`;
  - `poe vapid-keys` prints a key pair; settings `PUSH_*` and `RATE_LIMIT_PUSH`.
- Push subscriptions are owned data: exported without their keys, erased with the account. No
  user agent is stored.

### Added — Phase 10: admin console, audit, account security

- **Audit trail** (`app/platform/audit`, ADR-0017): every sensitive change is one entry. That
  covers roles, disabling, sign-outs, password resets, allowlist, modules, two-step sign-in,
  exports and deletions. Secret-looking details are redacted, and entries expire after
  `AUDIT_RETENTION_DAYS`. `GET /v1/admin/audit-logs` reads them with filters and resolves
  emails.
- **User management** (`/v1/admin/users`): search and filter, set a role, disable or enable,
  sign out everywhere, and reset a password to a one-time password that also turns two-step
  sign-in off. An admin never changes their own account here (`ADMIN_SELF_CHANGE`).
- **Allowlist management** (`/v1/admin/allowed-emails`): the env list plus a collection the
  console edits. Registration and Google sign-in accept both.
- **Module management** (`/v1/admin/modules`): stage and global switch as overrides of the
  manifest; `null` resets.
- **Admin corrections** (`/v1/admin/expenses/entries`): everyone's entries with the list's
  filters, and a correction with a reason. It is a revision by the admin (source `admin`) plus
  an `entry.admin_corrected` audit entry naming the fields, never their values.
- **Usage analytics** (`/v1/admin/analytics/usage`, `/db`):
  - sign-ups per day and active users (from sessions, so past ranges count right);
  - assistant answers by AI or rules, the fallback rate, and AI calls by provider;
  - errors by code per day, counted in memory and written in one batch at most every
    `ADMIN_ERROR_TALLY_FLUSH_SECONDS` (`error_tallies`, `ADMIN_ERROR_TALLY_DAYS`);
  - database size per collection, against `ADMIN_DB_QUOTA_MB`;
  - ranges default to `ADMIN_USAGE_DEFAULT_DAYS`, span at most `ADMIN_USAGE_MAX_DAYS`, and a
    range that can't be counted is `RANGE_INVALID`.
- **Two-step sign-in (TOTP)**:
  - RFC 6238 with the standard library;
  - the secret is encrypted with `AUTH_FIELD_ENCRYPTION_KEY` (Fernet), and recovery codes are
    hashed;
  - every code works once, and wrong codes count toward the lockout;
  - `/v1/auth/totp` (`setup`, `enable`, `disable`, `recovery-codes`), plus a challenge from
    `/login` and `/google` that `/v1/auth/login/totp` completes;
  - settings: `AUTH_TOTP_*` and `AUTH_RECOVERY_CODES`.
- **Proof it's really them** (`Reauth`): turning 2FA on or off, deleting the account, and a
  Google-only account's first password need the password, or a Google sign-in no older than
  `AUTH_GOOGLE_REAUTH_MAX_AGE_SECONDS` (`AUTH_GOOGLE_REAUTH_REQUIRED`).
- **Your data**:
  - `GET /v1/me/export` streams everything kept about you as one JSON file. A document's
    `PRIVATE_FIELDS` are left out, and so are who else acted on the account and from where.
  - `DELETE /v1/me` erases every record with your `user_id`, then the account. It needs the
    email typed, the password (or a fresh Google sign-in) and a code if 2FA is on. The last
    admin can't delete their own account.
- `docs/privacy-policy-template.md`, with the Play Store Data Safety answers.
- A test that every `/v1/admin` route answers 403 to users and 401 to anonymous callers.

### Changed — Phase 10

- `UserOut` has `two_factor`.
- `TimestampedDocument` has `PRIVATE_FIELDS`.
- `app.state.documents` lists every collection.
- The admin package is split by area.
- `/v1/auth/login/totp` is a public route, authenticated by its challenge token.
- `AUTH_SESSION_PURGE_GRACE_DAYS` replaces the fixed 7-day grace before ended sessions go.
- `poe reset-password` turns 2FA off and writes an audit entry; the seed's promotion of the
  bootstrap admin is audited too (no actor, `"via": "operator"`).

### Security — Phase 10 review

- A Google-only account could set a password from a stolen session, then use it to delete the
  account. Setting a first password now needs a fresh Google sign-in (and a code with 2FA on).
- Turning 2FA on needed no password, so a stolen session could lock the owner out. It now needs
  the first factor again.
- The lockout is checked inside the same conditional write that spends a code, so guesses sent
  all at once can't get past it.
- 2FA codes: spaces are ignored everywhere (not only when signing in), and only ASCII digits
  match, so a code typed in Devanagari or Tamil digits is a wrong code instead of a 500.
- `setup` and `enable` write conditionally: racing requests can't enable an unchecked secret.
- Two admins demoting each other, or deleting themselves, at the same moment can no longer leave
  no admin (`ADMIN_LAST_ADMIN`, `ACCOUNT_LAST_ADMIN`).
- Deletion sweeps the records before and after signing the account out, so a failure in the
  first sweep leaves an account that can be deleted again, and writes already in flight don't
  survive. It also removes the account's allowlist invitation.
- A mistyped `AUTH_*` secret (such as `AUTH_FIELD_ENCRYPTION_KEY`) is no longer printed in the
  startup error.
- Audit redaction reaches into lists.
- A module override is audited even when users see no change.

### Added — Phase 9b: data pack (in progress)

- **Saved views** (`/v1/expenses/saved-filters`): named filters of the entries list, a period
  kept by name or a chart's days, one view per name; limit in `data/listing.yaml`.
- **Bulk edit** (`POST /v1/expenses/entries/bulk-update`): category, add or take off a tag,
  delete and bring back for many entries; each is changed by the usual rules (revision source
  `bulk`), what can't be is skipped with a reason, and the activity feed notes it once.
- **Search** (`app/platform/search`, `GET /v1/search`): `@search` providers per module, asked
  at once with a time limit and a cap each (`SEARCH_*`, `RATE_LIMIT_SEARCH`); expenses finds
  saved views, loans, products and entries.
- **CSV export** (`GET /v1/expenses/export.csv`): the list's filters, streamed, UTF-8 with a
  byte-order mark, formula-safe text; row limit `export.max_rows`, `RATE_LIMIT_EXPORT`. CORS
  exposes `Content-Disposition`.
- **CSV import preview** (`POST /v1/expenses/import/preview`): reads a bank statement, a
  spreadsheet or the export (base64 in JSON), guesses the header row, columns, date format and
  amount signs (`data/imports.yaml`), and shows the rows as entries with problems and
  duplicates; nothing saved. `RATE_LIMIT_IMPORTS`.
- **CSV import** (`POST /v1/expenses/import/commit`): adds the importable rows in batches
  (`limits.commit_batch`), each once per file and row, as entries with the source `import`;
  the activity feed notes the import once.
- **Monthly report** (`GET /v1/expenses/reports/monthly`, `.csv`): a month against the one
  before, by category (with the change), income, places, people, days and the biggest expenses.
- **Chat with expenses**: `ExpenseQuery` compiled from a whitelist (`features/queries`,
  `POST /v1/expenses/query`, tool `expenses_query`), the `ask-expenses` skill (AI, or its static
  reading in English, Tamil and Hindi), and property tests of the compiler (seeded, no new
  dependency). Limits in `data/analytics.yaml` (`query`).
- **Other currencies** (ADR-0004): exchange rates from Frankfurter cached per currency and day
  (`app/platform/fx`, `GET /v1/fx/rates`, `FX_*`); an entry in another currency keeps
  `base_currency`, `fx_rate` and `amount_base_minor`, and every total counts that.

### Changed

- A repeating rule keeps adding in its own currency after the base currency changes, counted in
  the new base at each day's rate (it was refused before).

### Security

- **Request body limit**: every route refuses a body over `API_MAX_BODY_BYTES` (default 3 MB)
  with 413 `REQUEST_TOO_LARGE` before reading it, chunked bodies included.

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
