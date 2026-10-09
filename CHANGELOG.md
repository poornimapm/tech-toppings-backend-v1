# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
