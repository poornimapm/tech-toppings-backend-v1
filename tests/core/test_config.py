from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    PROJECT_ROOT,
    Environment,
    LogFormat,
    MongoSettings,
    default_env_files,
    get_settings,
    load_settings,
)

MANAGED_VARS = ("APP_ENV", "MONGO_URI", "LOG_LEVEL", "LOG_FORMAT", "CORS_ALLOWED_ORIGINS")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)


def test_later_env_files_override_earlier_and_real_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "MONGO_URI=mongodb://localhost:27017/from-base\nLOG_LEVEL=info\nLOG_FORMAT=console\n",
        encoding="utf-8",
    )
    specific = tmp_path / "test.env"
    specific.write_text("LOG_LEVEL=warning\n", encoding="utf-8")
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = load_settings([base, specific, tmp_path / "missing.env"])

    assert settings.mongo.uri.get_secret_value() == "mongodb://localhost:27017/from-base"
    assert settings.log.level == "WARNING"
    assert settings.log.format is LogFormat.JSON


def test_list_settings_accept_comma_separated_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/db")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", " http://a.test , http://b.test,, ")

    settings = load_settings([])

    assert settings.cors.allowed_origins == ("http://a.test", "http://b.test")


def test_defaults_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/db")

    settings = load_settings([])

    assert settings.app.env is Environment.DEVELOPMENT
    assert settings.cors.allowed_origins == ()
    assert settings.security.hsts_max_age_seconds == 0
    assert "password" in settings.log.redact_keys


def test_mongo_uri_is_required() -> None:
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


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/cached")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
