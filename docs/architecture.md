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
| Modules | `app.modules.<key>` | core, platform public surface |

`app.main` composes everything. import-linter (`pyproject.toml`) fails CI if a lower layer
imports a higher one.

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

They are hermetic: settings variables from the shell or `.env` are stripped. Apps are built
with explicit `Settings` through `tests/helpers.build_settings` and exercised in-process
(`asgi-lifespan` + `httpx.ASGITransport`).
