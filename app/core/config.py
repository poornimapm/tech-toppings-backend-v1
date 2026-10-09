"""Typed application settings.

All configuration enters the app here. Values come from environment variables; for local
development they may also come from ``.env`` and ``environments/<APP_ENV>.env`` (later files win,
real environment variables always win). Nothing else in the codebase reads ``os.environ``.

Each concern is its own ``BaseSettings`` group with its own prefix (``APP_``, ``MONGO_`` ...),
so ``.env.example`` documents every variable by group.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from limits import parse as parse_rate_limit
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
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
    ALLOWLIST = "allowlist"  # only emails in AUTH_ALLOWED_EMAILS (ADR-0011)
    CLOSED = "closed"


def _group_config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file_encoding="utf-8",
        extra="ignore",  # env files hold every group's variables
        frozen=True,
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
    model_config = _group_config("MONGO_")

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


class SecuritySettings(BaseSettings):
    model_config = _group_config("SECURITY_")

    # 0 disables the Strict-Transport-Security header (local http development).
    hsts_max_age_seconds: int = Field(default=0, ge=0)


class AuthSettings(BaseSettings):
    model_config = _group_config("AUTH_")

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


class RateLimitSettings(BaseSettings):
    model_config = _group_config("RATE_LIMIT_")

    enabled: bool = True
    # limits storage URI; in-memory suits a single instance (Render free).
    storage_uri: str = "async+memory://"
    auth_login: str = "10/minute"
    auth_register: str = "5/minute"
    auth_refresh: str = "60/minute"
    auth_password_change: str = "10/minute"  # noqa: S105 - a request rate, not a password

    @field_validator("auth_login", "auth_register", "auth_refresh", "auth_password_change")
    @classmethod
    def valid_rate(cls, value: str) -> str:
        parse_rate_limit(value)  # raises ValueError on e.g. "ten per minute"
        return value


class ApiSettings(BaseSettings):
    model_config = _group_config("API_")

    default_page_size: int = Field(default=20, ge=1)
    max_page_size: int = Field(default=100, ge=1)


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
    bootstrap: BootstrapSettings


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
        bootstrap=BootstrapSettings(_env_file=existing),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
