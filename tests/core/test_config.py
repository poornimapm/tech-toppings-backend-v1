from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    PROJECT_ROOT,
    Environment,
    LogFormat,
    MongoSettings,
    RegistrationMode,
    default_env_files,
    get_settings,
    load_settings,
)

MANAGED_VARS = (
    "APP_ENV",
    "MONGO_URI",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "CORS_ALLOWED_ORIGINS",
    "AUTH_JWT_SECRET",
    "AUTH_ALLOWED_EMAILS",
    "RATE_LIMIT_AUTH_LOGIN",
    "DEFAULT_TIMEZONE",
)
SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/db")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    return monkeypatch


def test_later_env_files_override_earlier_and_real_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "\n".join(
            [
                "MONGO_URI=mongodb://localhost:27017/from-base",
                "LOG_LEVEL=info",
                "LOG_FORMAT=console",
                f"AUTH_JWT_SECRET={SECRET}",
            ]
        ),
        encoding="utf-8",
    )
    specific = tmp_path / "test.env"
    specific.write_text("LOG_LEVEL=warning\n", encoding="utf-8")
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = load_settings([base, specific, tmp_path / "missing.env"])

    assert settings.mongo.uri.get_secret_value() == "mongodb://localhost:27017/from-base"
    assert settings.log.level == "WARNING"
    assert settings.log.format is LogFormat.JSON


def test_list_settings_accept_comma_separated_values(required_env: pytest.MonkeyPatch) -> None:
    required_env.setenv("CORS_ALLOWED_ORIGINS", " http://a.test , http://b.test,, ")
    required_env.setenv("AUTH_ALLOWED_EMAILS", "Asha@Example.com, ravi@example.com")

    settings = load_settings([])

    assert settings.cors.allowed_origins == ("http://a.test", "http://b.test")
    assert settings.auth.allowed_emails == ("asha@example.com", "ravi@example.com")


@pytest.mark.usefixtures("required_env")
def test_defaults_are_safe() -> None:
    settings = load_settings([])

    assert settings.app.env is Environment.DEVELOPMENT
    assert settings.cors.allowed_origins == ()
    assert settings.security.hsts_max_age_seconds == 0
    assert "password" in settings.log.redact_keys
    assert settings.auth.registration_mode is RegistrationMode.ALLOWLIST
    assert settings.auth.allowed_emails == ()
    assert settings.auth.refresh_cookie_secure is True
    assert settings.auth.refresh_cookie_samesite == "strict"
    assert settings.auth.google_client_id is None
    assert settings.rate_limit.enabled is True
    assert settings.bootstrap.email is None


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AUTH_JWT_SECRET", "too-short", "at least 32"),
        ("RATE_LIMIT_AUTH_LOGIN", "ten per minute", "auth_login"),
        ("DEFAULT_TIMEZONE", "Mars/Olympus", "unknown time zone"),
    ],
)
def test_invalid_values_are_rejected(
    required_env: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    required_env.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        load_settings([])


def test_mongo_uri_and_jwt_secret_are_required() -> None:
    with pytest.raises(ValidationError, match="uri"):
        load_settings([])


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("mongodb://localhost:27017", "database name"),
        ("mongodb://localhost:27017/", "database name"),
        ("postgres://localhost/db", "mongodb://"),
    ],
)
def test_mongo_uri_must_be_mongodb_with_database_name(uri: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MongoSettings(_env_file=None, uri=uri)  # type: ignore[arg-type]


def test_srv_uri_with_options_is_accepted() -> None:
    settings = MongoSettings(
        _env_file=None,
        uri="mongodb+srv://user:pw@cluster.example.net/tech-toppings?retryWrites=true",  # type: ignore[arg-type]
    )

    assert "cluster.example.net" in settings.uri.get_secret_value()
    assert "pw" not in repr(settings)


def test_default_env_files_follow_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")

    assert default_env_files() == (
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / "environments" / "production.env",
    )


@pytest.mark.usefixtures("required_env")
def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
