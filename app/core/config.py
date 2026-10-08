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
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


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


class Settings(BaseModel):
    """Root settings object; one attribute per configuration group."""

    model_config = ConfigDict(frozen=True)

    app: AppSettings
    mongo: MongoSettings
    log: LogSettings
    cors: CorsSettings
    security: SecuritySettings


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
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
