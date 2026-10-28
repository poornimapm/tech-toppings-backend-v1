"""Typed application settings.

All configuration enters the app here. Values come from environment variables; for local
development they may also come from ``.env`` and ``environments/<APP_ENV>.env`` (later files win,
real environment variables always win). Nothing else in the codebase reads ``os.environ``.

Each concern is its own ``BaseSettings`` group with its own prefix (``APP_``, ``MONGO_`` ...),
so ``.env.example`` documents every variable by group.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Sequence
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from limits import parse as parse_rate_limit
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Locale(StrEnum):
    """UI languages the platform supports (ADR-0009)."""

    EN = "en"
    TA = "ta"
    HI = "hi"


class RegistrationMode(StrEnum):
    OPEN = "open"
    ALLOWLIST = "allowlist"  # AUTH_ALLOWED_EMAILS plus those added in the admin console
    CLOSED = "closed"


def _group_config(prefix: str, *, secret: bool = False) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file_encoding="utf-8",
        extra="ignore",  # env files hold every group's variables
        frozen=True,
        hide_input_in_errors=secret,  # a startup error must not print a mistyped secret
    )


def _split_csv(value: Any) -> Any:
    """Accept ``a,b,c`` (env-friendly) as well as a real list."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class AppSettings(BaseSettings):
    model_config = _group_config("APP_")

    env: Environment = Environment.DEVELOPMENT
    name: str = "Tech-Toppings API"
    version: str = "0.1.0"
    docs_enabled: bool = True
    # Timeout for outbound HTTP calls (e.g. Google sign-in keys).
    http_timeout_seconds: float = Field(default=10.0, gt=0)


class MongoSettings(BaseSettings):
    model_config = _group_config("MONGO_", secret=True)  # the URI carries credentials

    # Full connection string including the database name,
    # e.g. mongodb://localhost:27017/tech-toppings-v1
    uri: SecretStr
    max_pool_size: int = Field(default=10, ge=1)
    min_pool_size: int = Field(default=0, ge=0)
    server_selection_timeout_ms: int = Field(default=5000, ge=100)
    connect_timeout_ms: int = Field(default=5000, ge=100)
    app_name: str = "tech-toppings-api"

    @field_validator("uri")
    @classmethod
    def require_database_name(cls, value: SecretStr) -> SecretStr:
        # Parsed without pymongo: mongodb+srv URIs would trigger a DNS lookup at import time.
        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"mongodb", "mongodb+srv"}:
            raise ValueError("must start with mongodb:// or mongodb+srv://")
        if not parts.path.strip("/"):
            raise ValueError("must include the database name, e.g. mongodb://host:27017/dbname")
        return value


class LogSettings(BaseSettings):
    model_config = _group_config("LOG_")

    level: str = "INFO"
    format: LogFormat = LogFormat.JSON
    # Keys whose values are masked anywhere in a log event (case-insensitive substring match).
    redact_keys: Annotated[tuple[str, ...], NoDecode] = (
        "password",
        "secret",
        "token",
        "authorization",
        "cookie",
        "transcript",
        "email",
        "totp",
        "sms",
        "uri",
    )
    # Successful requests to these paths are logged at DEBUG (health probes are frequent).
    quiet_paths: Annotated[tuple[str, ...], NoDecode] = ("/healthz", "/readyz")

    @field_validator("redact_keys", "quiet_paths", mode="before")
    @classmethod
    def split_lists(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("level")
    @classmethod
    def upper_level(cls, value: str) -> str:
        return value.upper()


class CorsSettings(BaseSettings):
    model_config = _group_config("CORS_")

    # Empty by default: production traffic arrives same-origin through the /api proxy.
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, value: Any) -> Any:
        return _split_csv(value)


PROXY_SECRET_MIN_LENGTH = 32


class SecuritySettings(BaseSettings):
    model_config = _group_config("SECURITY_", secret=True)  # the proxy secret

    # 0 disables the Strict-Transport-Security header (local http development).
    hsts_max_age_seconds: int = Field(default=0, ge=0)
    # Shared with the frontend's /api proxy, which sends it in `proxy_header` (ADR-0019). Set,
    # the API answers nothing else, but `proxy_exempt_paths` (the host's health check): nobody
    # reaches it around the proxy, so the client address the proxy passes on can be trusted.
    proxy_secret: SecretStr | None = None
    proxy_header: str = Field(default="X-TT-Proxy", pattern=r"^[A-Za-z0-9-]+$")
    proxy_exempt_paths: Annotated[tuple[str, ...], NoDecode] = ("/healthz",)

    @field_validator("proxy_secret", mode="before")
    @classmethod
    def blank_secret_is_none(cls, value: Any) -> Any:
        return value or None

    @field_validator("proxy_secret")
    @classmethod
    def strong_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            rf"[!-~]{{{PROXY_SECRET_MIN_LENGTH},}}", value.get_secret_value()
        ):
            raise ValueError(
                f"must be {PROXY_SECRET_MIN_LENGTH}+ printable ASCII characters, no spaces"
            )
        return value

    @field_validator("proxy_exempt_paths", mode="before")
    @classmethod
    def split_paths(cls, value: Any) -> Any:
        return _split_csv(value)


FERNET_KEY_BYTES = 32


def _fernet_key(value: SecretStr | None) -> SecretStr | None:
    """A Fernet key, if any: 32 bytes, url-safe base64."""
    if value is not None:
        try:
            decoded = base64.urlsafe_b64decode(value.get_secret_value().encode())
        except ValueError as exc:
            raise ValueError("must be 32 bytes, url-safe base64 (see .env.example)") from exc
        if len(decoded) != FERNET_KEY_BYTES:
            raise ValueError("must be 32 bytes, url-safe base64 (see .env.example)")
    return value


class AuthSettings(BaseSettings):
    model_config = _group_config("AUTH_", secret=True)

    # Signing key for access tokens. Generate one with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_issuer: str = "tech-toppings-api"
    jwt_audience: str = "tech-toppings-web"
    access_token_ttl_seconds: int = Field(default=900, ge=60)
    # Refresh sessions slide forward on every use but never outlive the absolute limit.
    refresh_token_ttl_seconds: int = Field(default=30 * 86400, ge=3600)
    refresh_absolute_ttl_seconds: int = Field(default=90 * 86400, ge=3600)
    # Two tabs refreshing at once: a just-rotated token inside this window is a race, not theft.
    refresh_reuse_grace_seconds: int = Field(default=10, ge=0)
    refresh_cookie_name: str = "tt_refresh"
    # Path as the browser sees it (requests arrive through the same-origin /api proxy).
    refresh_cookie_path: str = "/api/v1/auth"
    refresh_cookie_secure: bool = True
    refresh_cookie_samesite: Literal["strict", "lax"] = "strict"
    # Cookie-authenticated endpoints require this header (cross-site forms cannot send it).
    csrf_header: str = "X-TT-CSRF"

    registration_mode: RegistrationMode = RegistrationMode.ALLOWLIST
    allowed_emails: Annotated[tuple[str, ...], NoDecode] = ()

    password_min_length: int = Field(default=10, ge=8)
    password_max_length: int = Field(default=128, ge=16, le=1024)
    # Required character classes out of: lowercase, uppercase, digit, symbol.
    password_min_classes: int = Field(default=2, ge=1, le=4)

    lockout_threshold: int = Field(default=5, ge=1)
    lockout_base_seconds: int = Field(default=60, ge=1)
    lockout_max_seconds: int = Field(default=3600, ge=1)

    # Argon2id cost; defaults follow OWASP guidance and stay light enough for a 512 MB instance.
    argon2_time_cost: int = Field(default=2, ge=1)
    argon2_memory_kib: int = Field(default=19456, ge=8192)
    argon2_parallelism: int = Field(default=1, ge=1)

    # Google sign-in is enabled only when a client id is configured.
    google_client_id: str | None = None
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    google_issuers: Annotated[tuple[str, ...], NoDecode] = (
        "accounts.google.com",
        "https://accounts.google.com",
    )
    google_jwks_cache_seconds: int = Field(default=3600, ge=60)
    # TOTP 2FA: the key that encrypts each user's secret at rest (Fernet). Unset, 2FA can't be
    # turned on. Generate one with:
    #   python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    field_encryption_key: SecretStr | None = None
    totp_issuer: str = "Tech-Toppings"  # the name authenticator apps show
    totp_window_steps: int = Field(default=1, ge=0, le=2)  # 30-second steps either side
    totp_challenge_seconds: int = Field(default=300, ge=60, le=900)  # time to type the code
    recovery_codes: int = Field(default=10, ge=4, le=20)
    # Deleting an account or setting its first password without one: the Google sign-in given
    # as proof must be this recent.
    google_reauth_max_age_seconds: int = Field(default=300, ge=30, le=3600)
    # Ended sessions (device, IP) are kept this long past their end, to catch a stolen token.
    session_purge_grace_days: int = Field(default=7, ge=0, le=90)

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def normalise_emails(cls, value: Any) -> Any:
        items = _split_csv(value)
        return [item.lower() for item in items] if isinstance(items, list) else items

    @field_validator("google_issuers", mode="before")
    @classmethod
    def split_issuers(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("google_client_id")
    @classmethod
    def blank_is_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("field_encryption_key", mode="before")
    @classmethod
    def blank_key_is_none(cls, value: Any) -> Any:
        return value or None

    @field_validator("field_encryption_key")
    @classmethod
    def a_fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _fernet_key(value)


class RateLimitSettings(BaseSettings):
    model_config = _group_config("RATE_LIMIT_")

    enabled: bool = True
    # limits storage URI; in-memory suits a single instance (Render free).
    storage_uri: str = "async+memory://"
    auth_login: str = "10/minute"
    auth_register: str = "5/minute"
    auth_refresh: str = "60/minute"
    auth_password_change: str = "10/minute"  # noqa: S105 - a request rate, not a password
    agent_run: str = "20/minute"
    jobs_run: str = "30/minute"
    search: str = "120/minute"  # the palette asks as the user types
    export: str = "10/minute"  # each one reads many entries
    imports: str = "60/minute"  # each reads a whole file; an import asks once per batch
    push: str = "10/minute"  # subscribing a browser, or sending oneself a test push

    @field_validator(
        "auth_login",
        "auth_register",
        "auth_refresh",
        "auth_password_change",
        "agent_run",
        "jobs_run",
        "search",
        "export",
        "imports",
        "push",
    )
    @classmethod
    def valid_rate(cls, value: str) -> str:
        parse_rate_limit(value)  # raises ValueError on e.g. "ten per minute"
        return value


class ApiSettings(BaseSettings):
    model_config = _group_config("API_")

    default_page_size: int = Field(default=20, ge=1)
    max_page_size: int = Field(default=100, ge=1)
    # Largest request body any route takes; a bigger one is refused (413) before it is read.
    # The CSV import needs its file base64-encoded (a third bigger) plus its mapping.
    max_body_bytes: int = Field(default=3_000_000, ge=1024)
    # Answers this big or bigger are gzipped when the caller accepts it (0 = never): the free
    # host counts every byte the API sends (ADR-0019). Never those under `gzip_skip_paths`,
    # where a token is sent back: compressed next to what a caller sent, its length could
    # give it away (BREACH).
    gzip_min_bytes: int = Field(default=1000, ge=0)
    gzip_skip_paths: Annotated[tuple[str, ...], NoDecode] = ("/v1/auth",)

    @field_validator("gzip_skip_paths", mode="before")
    @classmethod
    def split_paths(cls, value: Any) -> Any:
        return _split_csv(value)


class DefaultsSettings(BaseSettings):
    """Defaults applied to new user accounts."""

    model_config = _group_config("DEFAULT_")

    locale: Locale = Locale.EN
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    timezone: str = "Asia/Kolkata"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value


class ModulesSettings(BaseSettings):
    """Where feature modules are discovered (ADR-0001). Each package's sub-packages that contain
    a ``manifest.py`` are modules; tests add a fixtures package."""

    model_config = _group_config("MODULES_")

    packages: Annotated[tuple[str, ...], NoDecode] = ("app.modules",)
    # A slow module must not hold up the Welcome page; its tile simply shows no figure.
    tile_stat_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("packages", mode="before")
    @classmethod
    def split_packages(cls, value: Any) -> Any:
        return _split_csv(value)


class AgentMode(StrEnum):
    AGENT = "agent"  # an AI agent runs the skill
    STATIC = "static"  # the skill's deterministic script runs; no AI involved


class ProviderSettings(BaseModel):
    """One OpenAI-compatible chat-completions endpoint (Gemini, Groq, OpenRouter, Ollama ...)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    base_url: str = Field(pattern=r"^https?://")
    model: str = Field(min_length=1)
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=800, ge=16, le=8192)


class AgentSettings(BaseSettings):
    """Skill-driven agents (ADR-0013). Mode and fallback are switches, so the app keeps working
    when free AI quota runs out: ``AGENT_MODE=static`` skips AI entirely, and with
    ``AGENT_FALLBACK=true`` a run that can't use AI falls back to the skill's static script
    (otherwise it answers honestly that AI is unavailable)."""

    model_config = _group_config("AGENT_")

    mode: AgentMode = AgentMode.AGENT
    fallback: bool = True
    # Per-skill mode overrides: "manage-modules=static,other-skill=agent".
    skill_modes: Annotated[dict[str, AgentMode], NoDecode] = Field(default_factory=dict)
    # Tried in order; a provider answering 429 rests for provider_cooldown_seconds.
    providers: tuple[ProviderSettings, ...] = ()
    provider_cooldown_seconds: int = Field(default=60, ge=1)
    # Packages whose sub-packages may contribute agents/, skills/ and tools.py (modules always do).
    packages: Annotated[tuple[str, ...], NoDecode] = ("app.platform",)
    max_steps: int = Field(default=6, ge=1, le=20)
    max_input_chars: int = Field(default=1000, ge=10, le=10000)
    user_daily_calls: int = Field(default=30, ge=0)
    global_daily_calls: int = Field(default=800, ge=0)
    run_retention_days: int = Field(default=7, ge=1)
    proposal_ttl_minutes: int = Field(default=30, ge=1)
    # Regexes masked out of user text before it reaches a provider (env value: a JSON list).
    redact_patterns: tuple[str, ...] = (
        r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*",  # emails and UPI ids
        r"\+?\d[\d\s-]{8,}\d",  # phone, account and card numbers
    )

    @field_validator("skill_modes", mode="before")
    @classmethod
    def parse_skill_modes(cls, value: Any) -> Any:
        if isinstance(value, str):
            pairs = (item.split("=", 1) for item in _split_csv(value))
            return {name.strip(): mode.strip() for name, mode in pairs}
        return value

    @field_validator("packages", mode="before")
    @classmethod
    def split_packages(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("redact_patterns")
    @classmethod
    def valid_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)  # fail at startup, not on the first request
            except re.error as exc:
                raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc
        return value

    def mode_for(self, skill: str) -> AgentMode:
        return self.skill_modes.get(skill, self.mode)


class DashboardSettings(BaseSettings):
    """The global dashboard and the activity feed behind it."""

    model_config = _group_config("DASHBOARD_")

    default_range_days: int = Field(default=30, ge=1)
    max_range_days: int = Field(default=366, ge=1)
    # A slow widget must not hold up the dashboard; its card says it couldn't load.
    widget_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    activity_retention_days: int = Field(default=180, ge=1)
    activity_widget_items: int = Field(default=8, ge=1, le=50)


class NotificationSettings(BaseSettings):
    model_config = _group_config("NOTIFICATIONS_")

    retention_days: int = Field(default=90, ge=1)


class JobsSettings(BaseSettings):
    """Scheduled work without a resident cron (ADR-0007): jobs run when the user's bell polls,
    and from the jobs-cron workflow through a token-protected endpoint."""

    model_config = _group_config("JOBS_", secret=True)

    # Shared with the jobs-cron workflow (header X-Job-Token); unset = the endpoint refuses
    # every call. Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    token: Annotated[SecretStr, Field(min_length=32)] | None = None
    # How long a daily job's run holds its lease; a run that fails is retried after it.
    lease_seconds: int = Field(default=300, ge=10)
    # Each job's time limit on the bell's poll (the user waits) and on a cron run.
    lazy_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    run_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    # The cron catches up users signed in within this many days, most recent first.
    active_days: int = Field(default=45, ge=1)
    max_users_per_run: int = Field(default=200, ge=1)
    retention_days: int = Field(default=30, ge=1)

    @field_validator("token", mode="before")
    @classmethod
    def blank_is_none(cls, value: Any) -> Any:
        return value or None

    @field_validator("token")
    @classmethod
    def header_safe(cls, value: SecretStr | None) -> SecretStr | None:
        # Request headers arrive as printable ASCII, so any other token could never match.
        if value is not None and not re.fullmatch(r"[!-~]+", value.get_secret_value()):
            raise ValueError("must be printable ASCII without spaces, e.g. secrets.token_urlsafe")
        return value

    @model_validator(mode="after")
    def lease_outlasts_a_run(self) -> JobsSettings:
        # A lease that ended mid-run would let a second catch-up start the same daily job.
        if self.lease_seconds <= max(self.lazy_timeout_seconds, self.run_timeout_seconds):
            raise ValueError(
                "JOBS_LEASE_SECONDS must be longer than JOBS_LAZY_TIMEOUT_SECONDS and "
                "JOBS_RUN_TIMEOUT_SECONDS"
            )
        return self


class RemindersSettings(BaseSettings):
    """Reminders of things due on a day (ADR-0012), told through the notification inbox."""

    model_config = _group_config("REMINDERS_")

    # Local hour (user's time zone) at which a reminder is sent on its day.
    send_hour: int = Field(default=9, ge=0, le=23)
    # Days a reminder is kept after its due day; older due dates are never scheduled again.
    retention_days: int = Field(default=90, ge=1)
    # At least a week: the app offers snoozes of up to 7 days.
    max_snooze_days: int = Field(default=30, ge=7)
    # Reminders sent per catch-up at most; the rest go out on the next one.
    deliver_batch: int = Field(default=50, ge=1)


class SearchSettings(BaseSettings):
    """Search from the command palette: each module finds the caller's own things."""

    model_config = _group_config("SEARCH_")

    # Shorter queries find nothing: too many things match one letter.
    min_chars: int = Field(default=2, ge=1)
    max_chars: int = Field(default=80, ge=1)
    # Things one module may answer with; a slow module is left out of the answer.
    max_hits: int = Field(default=8, ge=1, le=50)
    timeout_seconds: float = Field(default=2.0, gt=0, le=10)


class FxSettings(BaseSettings):
    """Exchange rates (Frankfurter: free, no key), cached in MongoDB per currency and day."""

    model_config = _group_config("FX_")

    provider_url: str = "https://api.frankfurter.dev/v1"
    # Today's rates are fetched again after this long; a past day's never change.
    cache_ttl_hours: int = Field(default=12, ge=1)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)


class AuditSettings(BaseSettings):
    """Who changed what (roles, accounts, 2FA, the allowlist, modules, exports, deletions)."""

    model_config = _group_config("AUDIT_")

    # Days an entry is kept (long: an audit trail answers questions months later).
    retention_days: int = Field(default=730, ge=30)


class AdminSettings(BaseSettings):
    """The admin console's usage pages."""

    model_config = _group_config("ADMIN_")

    # The database's size limit, to show how much is used (MongoDB Atlas M0: 512 MB).
    db_quota_mb: int = Field(default=512, ge=1)
    # Days a day's count of each error code is kept.
    error_tally_days: int = Field(default=400, ge=1)
    # Error counts are kept in memory and written at most this often (0 = at once).
    error_tally_flush_seconds: float = Field(default=30.0, ge=0, le=3600)
    # The usage page's span when none is asked for, and the longest it answers.
    usage_default_days: int = Field(default=30, ge=1)
    usage_max_days: int = Field(default=366, ge=1)


# Connections the shared outbound HTTP client (app/core/http.py) has open at most.
HTTP_MAX_CONNECTIONS = 100
# Of each job time limit, the seconds a push round leaves for the job's writes around it:
# claiming what is owed before the round, and noting and clearing what it pushed after.
PUSH_JOB_HEADROOM_SECONDS = 0.5


class PushSettings(BaseSettings):
    """Web Push (ADR-0018): reminders reach a device even when the app is closed. Off until both
    VAPID keys and a contact are set; `poe vapid-keys` makes a key pair."""

    model_config = _group_config("PUSH_", secret=True)  # holds the private key

    vapid_public_key: str | None = None
    vapid_private_key: SecretStr | None = None
    # How push services can reach whoever runs this server (RFC 8292): mailto: or https:.
    subject: str | None = Field(default=None, pattern=r"^(mailto:|https://)\S+$")
    # How long a push service keeps a message for a device that is offline (28 days at most).
    ttl_seconds: int = Field(default=86_400, ge=0, le=2_419_200)
    # How long one round of pushes may take, to every browser at once (pushes go out in a job,
    # so it must leave the job's time limits PUSH_JOB_HEADROOM_SECONDS), and how long one
    # browser of it may: a slower push service costs that browser's try, and the round still
    # finishes.
    timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    device_timeout_seconds: float = Field(default=1.5, gt=0, le=60)
    # Pushes a round has under way at most; the rest wait their turn, and a browser's deadline
    # starts only once its push is under way. Each holds a connection of the shared HTTP client.
    max_parallel: int = Field(default=20, ge=1, le=HTTP_MAX_CONNECTIONS)
    # Tries at pushing one reminder: one not finished on every browser goes again once its
    # claim (JOBS_LEASE_SECONDS) passes, this many times in all; the inbox still has it.
    max_tries: int = Field(default=3, ge=1, le=20)
    # Browsers per account that receive pushes; past this the oldest is let go.
    max_subscriptions: int = Field(default=10, ge=1, le=100)
    # How long the server's signed identification is valid (push services accept up to 24h).
    vapid_expiry_hours: int = Field(default=12, ge=1, le=24)
    # The push services a browser's endpoint may be on (a host, or any host under it): the
    # server sends to that URL, so it must never be one a user made up.
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = (
        "fcm.googleapis.com",  # Chrome, Android, Samsung Internet
        "updates.push.services.mozilla.com",  # Firefox
        "push.apple.com",  # Safari
        "notify.windows.com",  # Edge
    )

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def hosts_from_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("allowed_hosts")
    @classmethod
    def plain_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """An endpoint's host is compared with these as is, so ``FCM.googleapis.com``,
        ``.push.apple.com`` or ``https://fcm.googleapis.com/`` become the plain host name."""
        hosts = tuple(
            re.sub(r"^[a-z][a-z0-9+.-]*://", "", item.strip().lower()).strip("./") for item in value
        )
        for host in hosts:
            # A dot at least: a bare "com" would let in every host under it.
            if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
                raise ValueError(f"{host!r} is not a host name, e.g. fcm.googleapis.com")
        return hosts

    @field_validator("vapid_public_key", "vapid_private_key", "subject", mode="before")
    @classmethod
    def blank_is_none(cls, value: Any) -> Any:
        return value or None

    @model_validator(mode="after")
    def keys_pair_up(self) -> PushSettings:
        public, private = self.vapid_public_key, self.vapid_private_key
        if (public is None) != (private is None):
            raise ValueError(
                "set both PUSH_VAPID_PUBLIC_KEY and PUSH_VAPID_PRIVATE_KEY, or neither"
            )
        # Checked now: a wrong pair would only show as every push refused, and a malformed
        # private key as every send failing.
        if private is not None and self._public_of(private.get_secret_value()) != public:
            raise ValueError(
                "PUSH_VAPID_PUBLIC_KEY must be the public key of PUSH_VAPID_PRIVATE_KEY, "
                "both as `poe vapid-keys` prints them"
            )
        return self

    @model_validator(mode="after")
    def a_browser_fits_in_a_round(self) -> PushSettings:
        # A browser as slow as the round would cut every message short, not cost its own try.
        if self.device_timeout_seconds >= self.timeout_seconds:
            raise ValueError(
                "PUSH_DEVICE_TIMEOUT_SECONDS must be shorter than PUSH_TIMEOUT_SECONDS"
            )
        return self

    @staticmethod
    def _public_of(private: str) -> str | None:
        """The public key (an uncompressed P-256 point) of a 32-byte scalar, both unpadded
        base64url; None if it isn't one."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", private):
            return None
        scalar = int.from_bytes(base64.urlsafe_b64decode(private + "="), "big")
        try:
            key = ec.derive_private_key(scalar, ec.SECP256R1())
        except ValueError:  # zero, or not below the curve's order
            return None
        point = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(point).rstrip(b"=").decode()

    @property
    def available(self) -> bool:
        return self.vapid_private_key is not None and self.subject is not None


class BootstrapSettings(BaseSettings):
    """First admin account, created or promoted by `poe seed` (skipped when unset)."""

    model_config = _group_config("BOOTSTRAP_ADMIN_")

    email: EmailStr | None = None
    password: SecretStr | None = None
    name: str = "Administrator"

    @field_validator("email", mode="before")
    @classmethod
    def blank_email_is_none(cls, value: Any) -> Any:
        return value or None


class BackupSettings(BaseSettings):
    """Copies of the whole database, made and restored from the owner's own machine by
    `poe backup` and `poe restore` (ADR-0019); the server never uses these."""

    model_config = _group_config("BACKUP_", secret=True)

    # Where backups are written (relative to the project), one folder each. Git ignores it.
    dir: Path = Path("backups")
    # Fernet key that encrypts each backup (same shape as AUTH_FIELD_ENCRYPTION_KEY, but a key
    # of its own, kept somewhere other than the backups). Empty = written as plain files.
    encryption_key: SecretStr | None = None
    # Backups kept in `dir`; older ones are deleted after a new one is written.
    keep: int = Field(default=7, ge=1)
    # Collections left out, e.g. caches that refill themselves (comma-separated).
    skip_collections: Annotated[tuple[str, ...], NoDecode] = ()
    # Documents written to the database at a time when restoring.
    restore_batch: int = Field(default=500, ge=1, le=10_000)

    @field_validator("encryption_key", mode="before")
    @classmethod
    def blank_key_is_none(cls, value: Any) -> Any:
        return value or None

    @field_validator("encryption_key")
    @classmethod
    def a_fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _fernet_key(value)

    @field_validator("skip_collections", mode="before")
    @classmethod
    def split_names(cls, value: Any) -> Any:
        return _split_csv(value)

    @property
    def path(self) -> Path:
        return self.dir if self.dir.is_absolute() else PROJECT_ROOT / self.dir


class Settings(BaseModel):
    """Root settings object; one attribute per configuration group."""

    model_config = ConfigDict(frozen=True)

    app: AppSettings
    mongo: MongoSettings
    log: LogSettings
    cors: CorsSettings
    security: SecuritySettings
    auth: AuthSettings
    rate_limit: RateLimitSettings
    api: ApiSettings
    defaults: DefaultsSettings
    modules: ModulesSettings
    agent: AgentSettings
    dashboard: DashboardSettings
    notifications: NotificationSettings
    jobs: JobsSettings
    reminders: RemindersSettings
    search: SearchSettings
    fx: FxSettings
    audit: AuditSettings
    admin: AdminSettings
    push: PushSettings
    bootstrap: BootstrapSettings
    backup: BackupSettings

    @model_validator(mode="after")
    def production_is_hardened(self) -> Settings:
        # A production server refuses to start on a setting that would quietly weaken it.
        if self.app.env is not Environment.PRODUCTION:
            return self
        problems = [
            problem
            for problem, wrong in (
                (
                    "SECURITY_HSTS_MAX_AGE_SECONDS must be above 0",
                    self.security.hsts_max_age_seconds <= 0,
                ),
                (
                    "SECURITY_PROXY_SECRET must be set (the API answers only through /api)",
                    self.security.proxy_secret is None,
                ),
                ("AUTH_REFRESH_COOKIE_SECURE must be true", not self.auth.refresh_cookie_secure),
                ("APP_DOCS_ENABLED must be false", self.app.docs_enabled),
                ("LOG_FORMAT must be json", self.log.format is not LogFormat.JSON),
                ("RATE_LIMIT_ENABLED must be true", not self.rate_limit.enabled),
                (
                    "CORS_ALLOWED_ORIGINS may list only https:// origins",
                    any(not origin.startswith("https://") for origin in self.cors.allowed_origins),
                ),
            )
            if wrong
        ]
        if problems:
            raise ValueError("unsafe for APP_ENV=production: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def a_push_round_fits_in_a_job(self) -> Settings:
        # Pushes go out in a job, between claiming what is owed and clearing it: a round that
        # left those writes no time would have the job killed whenever it ran to its deadline,
        # its claims kept and the messages it finished pushed again.
        jobs = self.jobs
        limit = min(jobs.lazy_timeout_seconds, jobs.run_timeout_seconds)
        if self.push.available and self.push.timeout_seconds > limit - PUSH_JOB_HEADROOM_SECONDS:
            raise ValueError(
                "PUSH_TIMEOUT_SECONDS must be at most JOBS_LAZY_TIMEOUT_SECONDS and "
                f"JOBS_RUN_TIMEOUT_SECONDS minus {PUSH_JOB_HEADROOM_SECONDS:g} seconds, left for "
                "claiming the pushes before the round and clearing them after"
            )
        return self


def default_env_files() -> tuple[Path, ...]:
    app_env = os.environ.get("APP_ENV", Environment.DEVELOPMENT.value)
    return (PROJECT_ROOT / ".env", PROJECT_ROOT / "environments" / f"{app_env}.env")


def load_settings(env_files: Sequence[Path] | None = None) -> Settings:
    """Build settings from the environment plus optional env files (missing files are skipped)."""
    files = tuple(env_files) if env_files is not None else default_env_files()
    existing = tuple(path for path in files if path.is_file()) or None
    return Settings(
        app=AppSettings(_env_file=existing),
        mongo=MongoSettings(_env_file=existing),  # uri is required: comes from env or env file
        log=LogSettings(_env_file=existing),
        cors=CorsSettings(_env_file=existing),
        security=SecuritySettings(_env_file=existing),
        auth=AuthSettings(_env_file=existing),  # jwt_secret is required
        rate_limit=RateLimitSettings(_env_file=existing),
        api=ApiSettings(_env_file=existing),
        defaults=DefaultsSettings(_env_file=existing),
        modules=ModulesSettings(_env_file=existing),
        agent=AgentSettings(_env_file=existing),
        dashboard=DashboardSettings(_env_file=existing),
        notifications=NotificationSettings(_env_file=existing),
        jobs=JobsSettings(_env_file=existing),
        reminders=RemindersSettings(_env_file=existing),
        search=SearchSettings(_env_file=existing),
        fx=FxSettings(_env_file=existing),
        audit=AuditSettings(_env_file=existing),
        admin=AdminSettings(_env_file=existing),
        push=PushSettings(_env_file=existing),
        bootstrap=BootstrapSettings(_env_file=existing),
        backup=BackupSettings(_env_file=existing),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
