from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    HTTP_MAX_CONNECTIONS,
    PROJECT_ROOT,
    PUSH_JOB_HEADROOM_SECONDS,
    Environment,
    LogFormat,
    MongoSettings,
    RegistrationMode,
    default_env_files,
    get_settings,
    load_settings,
)
from app.platform.push.webpush import new_key_pair

MANAGED_VARS = (
    "APP_ENV",
    "MONGO_URI",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "CORS_ALLOWED_ORIGINS",
    "AUTH_JWT_SECRET",
    "AUTH_ALLOWED_EMAILS",
    "RATE_LIMIT_AUTH_LOGIN",
    "RATE_LIMIT_JOBS_RUN",
    "JOBS_TOKEN",
    "JOBS_LEASE_SECONDS",
    "JOBS_LAZY_TIMEOUT_SECONDS",
    "JOBS_RUN_TIMEOUT_SECONDS",
    "REMINDERS_SEND_HOUR",
    "DEFAULT_TIMEZONE",
    "PUSH_VAPID_PUBLIC_KEY",
    "PUSH_VAPID_PRIVATE_KEY",
    "PUSH_SUBJECT",
    "PUSH_TIMEOUT_SECONDS",
    "PUSH_DEVICE_TIMEOUT_SECONDS",
    "PUSH_MAX_TRIES",
    "PUSH_MAX_PARALLEL",
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
    assert settings.jobs.token is None  # the jobs endpoint refuses every call until it is set
    assert settings.bootstrap.email is None


def test_a_blank_job_token_means_none(required_env: pytest.MonkeyPatch) -> None:
    required_env.setenv("JOBS_TOKEN", "")

    assert load_settings([]).jobs.token is None


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AUTH_JWT_SECRET", "too-short", "at least 32"),
        ("RATE_LIMIT_AUTH_LOGIN", "ten per minute", "auth_login"),
        ("RATE_LIMIT_JOBS_RUN", "often", "jobs_run"),
        ("JOBS_TOKEN", "too-short", "at least 32"),
        ("JOBS_TOKEN", "é" * 40, "printable ASCII"),  # a request header could never match it
        ("JOBS_LEASE_SECONDS", "20", "must be longer than"),  # the cron's time limit is 20
        ("REMINDERS_SEND_HOUR", "24", "less than or equal to 23"),
        ("REMINDERS_MAX_SNOOZE_DAYS", "5", "greater than or equal to 7"),  # the app offers 7
        ("DEFAULT_TIMEZONE", "Mars/Olympus", "unknown time zone"),
        ("PUSH_DEVICE_TIMEOUT_SECONDS", "2.0", "shorter than PUSH_TIMEOUT_SECONDS"),
        ("PUSH_MAX_TRIES", "0", "greater than or equal to 1"),
        # More pushes under way than the shared HTTP client has connections would wait for one
        # on the browser's time.
        (
            "PUSH_MAX_PARALLEL",
            str(HTTP_MAX_CONNECTIONS + 1),
            f"less than or equal to {HTTP_MAX_CONNECTIONS}",
        ),
    ],
)
def test_invalid_values_are_rejected(
    required_env: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    required_env.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        load_settings([])


@pytest.fixture
def push_on(required_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    public, private = new_key_pair()
    required_env.setenv("PUSH_VAPID_PUBLIC_KEY", public)
    required_env.setenv("PUSH_VAPID_PRIVATE_KEY", private)
    required_env.setenv("PUSH_SUBJECT", "mailto:ops@example.com")
    return required_env


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # Shorter than the bell's poll gives a job (3.0), but too little left to clear it.
        ("PUSH_TIMEOUT_SECONDS", "2.9"),
        ("JOBS_LAZY_TIMEOUT_SECONDS", "2.4"),  # the round takes 2.0
        ("JOBS_RUN_TIMEOUT_SECONDS", "2.4"),
    ],
)
def test_a_push_round_must_leave_its_job_time_to_clear(
    push_on: pytest.MonkeyPatch, name: str, value: str
) -> None:
    push_on.setenv(name, value)

    with pytest.raises(
        ValidationError, match="PUSH_TIMEOUT_SECONDS must be at most JOBS_"
    ) as caught:
        load_settings([])
    assert f"minus {PUSH_JOB_HEADROOM_SECONDS:g} seconds" in str(caught.value)


def test_a_push_round_may_take_all_but_the_headroom(push_on: pytest.MonkeyPatch) -> None:
    lazy = load_settings([]).jobs.lazy_timeout_seconds
    push_on.setenv("PUSH_TIMEOUT_SECONDS", str(lazy - PUSH_JOB_HEADROOM_SECONDS))

    assert load_settings([]).push.timeout_seconds == lazy - PUSH_JOB_HEADROOM_SECONDS


def test_a_server_that_cannot_push_may_run_jobs_quicker(required_env: pytest.MonkeyPatch) -> None:
    required_env.setenv("JOBS_LAZY_TIMEOUT_SECONDS", "1.0")

    assert load_settings([]).jobs.lazy_timeout_seconds == 1.0


@pytest.mark.usefixtures("push_on")
def test_push_defaults_fit_in_the_jobs() -> None:
    settings = load_settings([])

    assert settings.push.available
    assert settings.push.device_timeout_seconds < settings.push.timeout_seconds
    room = settings.jobs.lazy_timeout_seconds - settings.push.timeout_seconds
    assert room >= PUSH_JOB_HEADROOM_SECONDS
    assert settings.push.max_parallel <= HTTP_MAX_CONNECTIONS


@pytest.mark.parametrize(
    "name", ["JOBS_TOKEN", "AUTH_FIELD_ENCRYPTION_KEY", "AUTH_JWT_SECRET", "MONGO_URI"]
)
def test_a_rejected_secret_is_not_echoed(required_env: pytest.MonkeyPatch, name: str) -> None:
    required_env.setenv(name, "mistyped-SECRET")

    with pytest.raises(ValidationError) as caught:
        load_settings([])

    assert "SECRET" not in str(caught.value)


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
