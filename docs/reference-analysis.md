# Reference analysis — backend

This is a read-only study of an existing internal FastAPI backend (the "reference"). It records
which **structural** patterns Tech-Toppings reuses and which it avoids.

What was not taken from the reference:
- code, configuration values, environment files, credentials, endpoints or prompts;
- business logic, domain content or names.

The reference uses paid model providers; none of them are used here. The frontend study is in
`tech-toppings-frontend-v1/docs/reference-analysis.md`.

## 1. Snapshot

| Area | Reference | Tech-Toppings decision |
|---|---|---|
| App creation | Module-level `app = FastAPI(...)`; routers registered by hand in one aggregated router | `create_app(settings)` factory; platform routers plus **manifest-discovered** module routers |
| Layering | Router → (optional controller) → service → repository. Two service styles (module functions and classes). Only 3 controllers exist | Router → service → repository. No controller layer: response models replace it. One style: classes with constructor injection |
| Data access | Raw Motor + Pydantic `MongoBaseModel`. No base repository; each repository re-implements serialization and ObjectId conversion; services sometimes query `db[...]` directly | Beanie documents on `BaseDocument`/`OwnedDocument`. Generic `ScopedRepository[T]` is the **only** path to the DB from modules |
| User scoping | Manual, per query; several update-by-id paths without ownership checks | Scope applied automatically by the repository; the route-matrix isolation test fails if any route leaks |
| Config | pydantic-settings; `APP_ENV` picks `environments/<env>.env`; one 636-line settings class; vendored code also reads `os.environ` directly | Same `APP_ENV` file selection, but settings split into **nested groups per concern**; `os.getenv` banned by a lint rule outside `core/config.py` |
| Logging | stdlib text logs; rotating files per subsystem; no request ID; no JSON | structlog JSON to stdout; request-ID middleware; secret/PII redaction; no log files (Render disk is ephemeral) |
| Errors | `ApiResponse{success,message,data}` envelope with global handlers. The custom exception classes are never raised: `HTTPException` is raised from services and repositories (238 sites) | Error envelope `{code, message, details, request_id}`; domain `AppError` subclasses with stable codes; `HTTPException` is banned outside routers by lint |
| Auth | External identity provider tokens checked per request; role-check dependency defined but unused; some routers registered without auth | Own JWT + rotating refresh tokens; `require_role` / permission dependencies on every protected router; a test asserts that every `/v1` route except an allowlist requires auth |
| AI | Provider tier enum + config dataclass + builder table + `run_with_fallback` loop that skips retriable errors; structured-output validation; tolerant JSON salvage | Same chain idea, as a small provider-agnostic `AIService` over two free providers, plus quota, cache, PII redaction and a rule-based fallback owned by each module |
| Usage tracking | ContextVar attribution → never-raise recorder → bounded queue → batch writer → pluggable sinks | Simplified: per-call `ai_calls` documents (TTL) and per-day `ai_usage` counters, written in the request (low volume, single instance) |
| Realtime | python-socketio with rooms, an event-name constant class and a stream emitter (start/chunk/end/error); **no auth on connect** | Not needed in v1 (request/response with polling via TanStack Query). If added later: authenticate on connect, same emitter shape |
| Migrations | Idempotent seed functions; Mongo-based lock so one worker seeds; `$set` + `$setOnInsert` upserts. Disabled by a code comment | `scripts/seed.py` uses the same idempotent upsert pattern, run explicitly (`poe seed`), never silently at startup |
| Tests | ~130 unit files; `dependency_overrides` for auth/services; hand-written fakes; an autouse fixture that fails on live LLM calls; an `integration` marker. A nested suite sits outside `testpaths`, so it never runs | Same `dependency_overrides` and "no live LLM" guard; real Mongo test DB; one `testpaths` covering `tests/` and `app/modules/*/tests` |
| Container/CI | Single-stage slim Python image, non-root user, health check, thorough `.dockerignore`. CI build step does nothing; no lint, type check or lockfile | Multi-stage slim image with uv, non-root user, health check. GitHub Actions: ruff, mypy `--strict`, import-linter, pytest + coverage, pip-audit, gitleaks, docker build |

## 2. Patterns worth reusing (structure only)

1. **Lifespan ordering.**
   - Startup: connect, then idempotent `ensure_indexes`, then start background helpers.
   - Optional dependencies log a warning and degrade instead of crashing.
   - Shutdown runs in reverse order.

   Tech-Toppings follows this, with Beanie `init_beanie` building the indexes.
2. **Settings helpers.**
   - Derived `@property` values (CSV → list).
   - A validator that turns a blank environment value back into the default.
   - `@lru_cache get_settings()` for dependency injection and test overrides.
3. **Central exception-handler registration.**
   - `register_exception_handlers(app)` maps each exception family to an HTTP status.
   - Validation errors are flattened into readable `field.path: message` details.
4. **Provider fallback loop.**
   - An ordered chain of providers.
   - A provider is skipped on retriable errors (rate limit, 5xx, timeout), found by walking the
     `__cause__`/`__context__` chain.
   - A `NonRetriableError` stops the chain.
   - Each attempt is wrapped in timing and logging.
5. **Structured-output validation with salvage.**
   - Strict `model_validate_json` first.
   - Then lenient JSON parsing.
   - Then per-record validation that keeps the good records.

   Tech-Toppings uses this for multi-expense utterances, so one bad item does not drop the rest.
6. **Context-bound tool state.** A `ContextVar` set before the run and reset in `finally` keeps
   tool functions free of arguments the model could get wrong. Reused for request context
   (`request_id`, `user_id`) in logging.
7. **Background tasks held in a strong-reference set** with a done-callback that logs failures.
   This avoids silently garbage-collected tasks. Reused for fire-and-forget notifications.
8. **Never-raise telemetry.** Usage/audit recording must never fail the user's request: it
   logs and moves on.
9. **Seed idempotency.** Upserts with `$setOnInsert` for defaults, so re-running the seed
   never overwrites user changes.
10. **Test guards.**
    - An autouse fixture that fails the test if a live LLM provider is reached.
    - `dependency_overrides` for the auth user.
    - An `integration` marker skipped unless the environment provides the dependency.
11. **Pagination bounds.**
    - Page size clamped by `Field(ge=1, le=MAX)`.
    - Items and count fetched separately.

    Tech-Toppings generalizes this into one `Page[T]` used by every list endpoint.
12. **Container hygiene.**
    - Non-root UID.
    - `PYTHONUNBUFFERED` and `PYTHONDONTWRITEBYTECODE`.
    - Health-check instruction.
    - Deny-list `.dockerignore` excluding env files, keys, logs and tests.

## 3. Anti-patterns to avoid, and the guard that prevents each

| Seen in the reference | Guard in Tech-Toppings |
|---|---|
| Routers with destructive operations registered **without auth** | Auth is the default on the `/v1` router; public routes come from an explicit allowlist, checked by a test |
| Ownership checks missing on update/delete by ID | `ScopedRepository` has no unscoped methods; `Scope.all()` can only be built by the admin router dependency; route-matrix isolation tests |
| Role-check dependency defined but never used | Each router declares its required role or permission; a test lists every route with its guard |
| `HTTPException` raised from services and repositories | Lint rule (ruff banned-api) forbids `fastapi.HTTPException` outside `routers`; services raise `AppError` subclasses |
| Repositories swallow DB errors and return empty results | Repositories never catch broad exceptions; the global handler returns `DB_UNAVAILABLE` (503) |
| Health endpoint returns 200 when the DB is down | `/readyz` returns 503 with the envelope when Mongo ping fails; `/healthz` is process-only |
| `success` derived from `data is not None` | No success flag: the HTTP status carries success; errors use the envelope |
| Unbounded, unsigned token handling; TLS checks disabled in code | PyJWT with signature verification, `exp`/`iat`/`typ` checks; TLS settings only from the connection string |
| No request IDs, plain-text logs, local log files | Request-ID middleware, JSON logs to stdout, redaction processor |
| 636-line settings class, duplicate fields, `os.environ` everywhere | Settings split per concern; `os.getenv` banned outside config; duplicate keys caught by tests |
| Sixteen files over 1,000 lines; mixed camelCase/snake_case files; lowercase class names | Ruff naming rules (`N`), `max-module-lines` budget checked in CI, one feature per folder |
| Synchronous SDK calls inside `async def` (blocks the event loop) | Only async clients (`httpx.AsyncClient`, Motor); a ruff rule (`ASYNC`) flags blocking calls |
| Local disk state (indexes, spools, JSONL logs) | Nothing written to disk at runtime: imports are parsed in memory, exports streamed |
| Unpinned `requirements.txt`, no lockfile, process manager not declared | `pyproject.toml` + `uv.lock`; uvicorn declared and used in the container `CMD` |
| CI step that only echoes; no lint or type check; README left as a template | Full CI gates (§10 of plan.md); README run steps re-verified at every phase |
| Seeds/migrations disabled by commenting code; destructive call left beside them | Explicit `poe seed` command; no destructive seed paths; demo data behind `--demo` |
| Tests that hit a stale response shape; a nested test suite that never runs | Single `testpaths`; response shapes come from shared schema models; CI fails on 0 collected tests per package |
| Socket connections trust a client-supplied user ID | Any future realtime channel authenticates with the access token on connect |

## 4. What this means for the plan

- The **outbox-in-document revision protocol** (plan §5) replaces transactions. This mirrors
  the reference's tolerance for partial failure, but makes recovery explicit and tested.
- The **AI chain** reuses the reference's fallback-loop shape with free providers only. The
  reference's paid tiers, prompts and skill files are not reused.
- The **usage pipeline** is simplified on purpose: a single Render instance handles low call
  volume, so queues and sinks would be dead weight.
