# tech-toppings-backend-v1

This is the backend API for **Tech-Toppings**, a modular, multi-user personal platform. One
shell app hosts independent modules, and Module 1 is a voice-first expense tracker. It is a
FastAPI service on MongoDB, run as a modular monolith.

- Frontend: <https://github.com/poornimapm/tech-toppings-frontend-v1>
- Plan and decisions: [docs/plan.md](docs/plan.md), [docs/adr/](docs/adr/)
- Architecture overview: [docs/architecture.md](docs/architecture.md)

> **Status:** Phase 2 (auth, users, RBAC) is done. Endpoints: health probes, `/v1/auth/*`,
> `/v1/me` and `/v1/me/sessions`.

## Stack

| Concern | Choice |
|---|---|
| Language and runtime | Python 3.11 in a **project-level venv** (`.venv`) |
| Web | FastAPI on uvicorn |
| Database | MongoDB 8 via **PyMongo Async** + **Beanie 2** ODM (local mongod; Atlas M0 in production) |
| Config | pydantic-settings, one prefixed group per concern (see [.env.example](.env.example)) |
| Logging | structlog: JSON to stdout, request IDs, secret redaction |
| Quality | ruff, mypy `--strict`, import-linter, pytest + coverage, pip-audit, gitleaks |
| Tasks | poethepoet (`poe <task>`), cross-platform with no `make` needed |

## Prerequisites

- **Python 3.11**: `py -3.11 --version` on Windows.
- **MongoDB 8.x** reachable at `mongodb://localhost:27017`.
- Optional: Docker Desktop, only for `docker compose up`.

## Setup (first time)

All packages are installed into `.venv` inside this folder. Nothing is installed globally.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements\dev.lock
.venv\Scripts\pip install --no-deps --editable .
copy .env.example .env            # then set AUTH_JWT_SECRET (see below) and adjust values
.venv\Scripts\pre-commit install  # optional: run lint/type checks on every commit
```

On macOS or Linux, use `python3.11` and `.venv/bin/` instead of `.venv\Scripts\`.

Then edit `.env`:

1. `AUTH_JWT_SECRET`: paste the output of
   `.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. `AUTH_ALLOWED_EMAILS`: the emails that may register (comma-separated). Registration is
   allowlist-only by default.
3. `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`: your admin account. Create it
   with `.venv\Scripts\poe seed`.

> **Tip:** you can activate the venv with `.venv\Scripts\Activate.ps1` and then type `poe dev`.
> If PowerShell blocks scripts, call the tools directly, for example `.venv\Scripts\poe dev`.

## Run

```powershell
.venv\Scripts\poe dev
```

This serves the API at <http://127.0.0.1:8000> with auto-reload. `HOST` and `PORT` come from
`.env`.

### Verify

| Check | Expected |
|---|---|
| <http://127.0.0.1:8000/healthz> | `200 {"status":"ok","name":"Tech-Toppings API","version":"0.1.0"}` |
| <http://127.0.0.1:8000/readyz> | `200 {"status":"ready","checks":{"mongo":"ok"}}` |
| `/readyz` with MongoDB stopped | `503 {"code":"SERVICE_NOT_READY",...,"request_id":"..."}`. The app keeps running and recovers by itself when MongoDB is back. |
| <http://127.0.0.1:8000/docs> | Interactive OpenAPI docs |
| Any unknown path | `404 {"code":"NOT_FOUND","message":...,"details":null,"request_id":...}` |

## Accounts and sign-in

| Endpoint | Purpose |
|---|---|
| `GET /v1/auth/config` | Public: registration mode, password policy, Google client id |
| `POST /v1/auth/register` | Allowlisted emails only (`AUTH_REGISTRATION_MODE`). Signs in on success |
| `POST /v1/auth/login` | Email + password. Locks the account with growing backoff after repeated failures |
| `POST /v1/auth/refresh` | Rotates the httpOnly refresh cookie and returns a new access token. Needs the `X-TT-CSRF` header |
| `POST /v1/auth/logout`, `/logout-all` | Sign out this device, or every device (which also invalidates access tokens) |
| `POST /v1/auth/password/change` | Signs out other devices. Required after an admin reset |
| `POST /v1/auth/google` | Optional "Sign in with Google" (set `AUTH_GOOGLE_CLIENT_ID`) |
| `GET/PATCH /v1/me` | Own profile: name, language, theme, currency, time zone, AI consent |
| `GET /v1/me/sessions`, `DELETE /v1/me/sessions/{id}` | Signed-in devices; sign one out |

- **Tokens.** Access tokens last 15 minutes and are kept in memory by the frontend. The
  refresh token exists only as a cookie (`HttpOnly; SameSite=Strict`, path `/api/v1/auth`).
  Reusing an old refresh token signs that device out (ADR-0003).
- **No email in v1.** If someone forgets their password, run
  `.venv\Scripts\poe reset-password --email them@example.com`. It prints a one-time
  password once and signs them out everywhere; they must choose a new password at the next
  sign-in.
- **Try it:** open `/docs`, call `POST /v1/auth/register` with an allowlisted email, copy the
  `access_token`, click **Authorize**, then call `GET /v1/me`.

Every response carries an `X-Request-ID` header. Send your own (8–128 characters from
`[A-Za-z0-9._-]`) and it is echoed back and included in every log line for that request.

## Tasks

| Command | What it does |
|---|---|
| `poe dev` | Run the API with reload |
| `poe test` | pytest with coverage (gate: 80%). Needs MongoDB; uses a throwaway database |
| `poe lint` | ruff check, format check, mypy `--strict`, import-linter |
| `poe fmt` | Auto-format and auto-fix |
| `poe audit` | pip-audit on the pinned runtime dependencies |
| `poe actionlint` | Lint the GitHub Actions workflow |
| `poe check` | **Everything CI runs**: lint, test, audit, actionlint |
| `poe lock` | Re-pin `requirements/*.lock` from `pyproject.toml` |
| `poe sync` | Make `.venv` match `requirements/dev.lock` exactly |
| `poe seed` | Create or promote the bootstrap admin (idempotent) |
| `poe reset-password --email …` | Admin password reset: prints a one-time password, forces a change |
| `poe openapi` | Write `openapi.json` for the frontend type generator |
| `poe docker-build` | Build the production image locally |

Prefix each command with `.venv\Scripts\` if the venv is not activated.

**Tests and MongoDB.** By default the tests use `mongodb://localhost:27017`. Set
`TEST_MONGO_URI` in `.env` or in your shell to use another server. Each run creates a
database named `tt-test-<random>` and drops it afterwards. Settings variables from your
shell or `.env` are ignored during tests, so results are reproducible.

## Dependencies

- Add or change a dependency in `pyproject.toml`, then run `poe lock` and `poe sync`.
- Both lock files are committed: `requirements/base.lock` (runtime, used by Docker) and
  `requirements/dev.lock` (development and CI).

## Docker (production parity)

```powershell
docker compose up --build   # API on :8000, MongoDB 8.0 on host port 27018
```

The image is multi-stage, runs as a non-root user, has a health check, and logs JSON. Render
injects `PORT`.

## Project layout

```
app/
  main.py            create_app() factory: logging, DB, middleware, error handlers, routers
  core/              plumbing with no knowledge of users or modules
    config.py        typed settings groups (the only place that reads the environment)
    logging.py       structlog setup + redaction
    errors.py        AppError hierarchy + {code,message,details,request_id} envelope
    middleware.py    request id + access log, security headers, CORS
    db.py            PyMongo async client, lazy Beanie init, readiness
    deps.py          FastAPI dependencies (settings, database, clock) from app.state
    documents.py     base documents (timestamps; owned = user_id + module_key + soft delete)
    repository.py    ScopedRepository: every query filtered to the caller's Scope
    pagination.py    page/page_size/sort parameters and the Page[T] response
    security.py      Argon2id hashing, password policy, JWT, token digests
    rate_limit.py    per-client rate limits (limits library)
  platform/          shared capabilities used by every module
    auth/            register/login/refresh/logout, sessions, Google, lockout, operator tasks
    users/           User model, /v1/me
    health/          /healthz, /readyz
  modules/           feature modules (Phase 3+), auto-discovered
tests/               pytest suite (real MongoDB)
scripts/             seed.py, reset_password.py, export_openapi.py, hooks/venv-run.sh
docs/                plan, ADRs, architecture, reference analysis, free-tier limits
```

## Conventions (enforced by tooling)

- **Errors:** raise `app.core.errors.AppError` subclasses. `HTTPException` is banned by
  ruff. Every error response has the envelope shape.
- **Configuration:** only `app/core/config.py` reads the environment. `os.environ` and
  `os.getenv` are banned elsewhere.
- **Layers:** `app.core` must not import `app.platform`, and platform must not import
  modules. import-linter enforces this in CI.
- **Data access:** user-owned records are read and written only through `ScopedRepository`.
  Another user's record answers 404, never 403. `Scope.unrestricted()` is allowed only under
  `app/platform/admin/`, and a test enforces this.
- **Auth by default:** every `/v1` route needs a bearer token unless it is on the short public
  list in `tests/architecture/test_access_rules.py`. The isolation matrix
  (`tests/isolation/`) must cover every route that takes an id.
- **Logs:** values whose key contains `password`, `token`, `cookie`, `email` and similar are
  masked automatically (`LOG_REDACT_KEYS`).
- **Commits:** never commit `.env`. The pre-commit hook blocks it, and gitleaks scans in CI.
- **Line endings:** files are stored as LF in git (`.gitattributes`).

## CI

`.github/workflows/ci.yml` runs on every push and pull request to `develop` and `main`:
- the quality job (lint, test against a `mongo:8.0` service, pip-audit, actionlint);
- a gitleaks secret scan;
- a Docker image build.
