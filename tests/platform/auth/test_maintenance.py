from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import BootstrapSettings, Settings, get_settings
from app.core.security import PasswordHasher
from app.platform.auth.maintenance import (
    BootstrapOutcome,
    ensure_bootstrap_admin,
    platform_database,
    reset_password,
)
from app.platform.users.models import Role, User
from tests.helpers import (
    CSRF,
    TEST_JWT_SECRET,
    UNREACHABLE_MONGO_URI,
    FrozenClock,
    build_app,
    build_settings,
    login,
    mongo_settings,
    register,
    running_client,
    unique_email,
)

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
ADMIN_PASSWORD = "Admin-Password-42"


def with_bootstrap(settings: Settings, email: str, password: str = ADMIN_PASSWORD) -> Settings:
    bootstrap = BootstrapSettings(_env_file=None, email=email, password=SecretStr(password))
    return settings.model_copy(update={"bootstrap": bootstrap})


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.usefixtures("client")  # the running app has initialised the database
async def test_bootstrap_admin_lifecycle(app: FastAPI, clock: FrozenClock) -> None:
    hasher = PasswordHasher(app.state.settings.auth)
    email = unique_email("admin")
    settings = with_bootstrap(app.state.settings, email)

    skipped = await ensure_bootstrap_admin(app.state.settings, clock, hasher)
    created = await ensure_bootstrap_admin(settings, clock, hasher)
    unchanged = await ensure_bootstrap_admin(settings, clock, hasher)

    assert (skipped, created, unchanged) == (
        BootstrapOutcome.SKIPPED,
        BootstrapOutcome.CREATED,
        BootstrapOutcome.UNCHANGED,
    )
    admin = await User.find_one({"email": email})
    assert admin is not None
    assert admin.role is Role.ADMIN


async def test_bootstrap_promotes_an_existing_account(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    settings = with_bootstrap(app.state.settings, account.email)

    outcome = await ensure_bootstrap_admin(settings, clock, PasswordHasher(settings.auth))

    assert outcome is BootstrapOutcome.PROMOTED
    signed_in = await login(client, account.email)  # password untouched
    assert signed_in.json()["user"]["role"] == "admin"


@pytest.mark.usefixtures("client")
async def test_bootstrap_refuses_a_weak_password(app: FastAPI, clock: FrozenClock) -> None:
    settings = with_bootstrap(app.state.settings, unique_email("admin"), "weak")

    with pytest.raises(ValueError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        await ensure_bootstrap_admin(settings, clock, PasswordHasher(settings.auth))


async def test_reset_password_signs_out_everywhere_and_clears_lockout(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)},
        {"$set": {"failed_logins": 9, "locked_until": clock.now()}},
    )

    one_time = await reset_password(
        account.email, app.state.settings, clock, PasswordHasher(app.state.settings.auth)
    )

    assert len(one_time) == 19  # xxxx-xxxx-xxxx-xxxx
    assert (await client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401
    assert (await client.get("/v1/me", headers=account.headers)).status_code == 401
    assert (await login(client, account.email)).status_code == 401
    signed_in = await login(client, account.email, one_time)
    assert signed_in.status_code == 200
    assert signed_in.json()["user"]["must_change_password"] is True


@pytest.mark.usefixtures("client")
async def test_reset_password_for_unknown_email(app: FastAPI, clock: FrozenClock) -> None:
    with pytest.raises(LookupError):
        await reset_password(
            unique_email("ghost"),
            app.state.settings,
            clock,
            PasswordHasher(app.state.settings.auth),
        )


async def test_platform_database_refuses_to_run_without_mongo(settings: Settings) -> None:
    offline = settings.model_copy(
        update={"mongo": mongo_settings(UNREACHABLE_MONGO_URI, server_selection_timeout_ms=200)}
    )

    with pytest.raises(RuntimeError, match="MONGO_URI"):
        async with platform_database(offline):
            pass  # pragma: no cover


@pytest.fixture
def script_env(monkeypatch: pytest.MonkeyPatch, mongo_uri: str) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("MONGO_URI", mongo_uri)
    monkeypatch.setenv("AUTH_JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("AUTH_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("AUTH_ARGON2_MEMORY_KIB", "8192")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


async def test_seed_and_reset_scripts(
    script_env: pytest.MonkeyPatch, mongo_uri: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Scripts run as their own processes in real use. Beanie binds models process-wide to the
    # last client that initialised it, so the app is started only after the scripts finished.
    admin_email = unique_email("seeded")
    script_env.setenv("BOOTSTRAP_ADMIN_EMAIL", admin_email)
    script_env.setenv("BOOTSTRAP_ADMIN_PASSWORD", ADMIN_PASSWORD)
    get_settings.cache_clear()

    assert await load_script("seed").run() == 0
    assert "bootstrap admin: created" in capsys.readouterr().out
    assert await load_script("reset_password").run(["--email", admin_email]) == 0
    one_time = capsys.readouterr().out.split(": ", 1)[1].splitlines()[0]
    assert await load_script("reset_password").run(["--email", unique_email("nobody")]) == 1
    assert "No account with that email" in capsys.readouterr().out

    async with running_client(build_app(build_settings(mongo_uri))) as http:
        signed_in = await login(http, admin_email, one_time)

    assert signed_in.status_code == 200
    assert signed_in.json()["user"]["role"] == "admin"
    assert signed_in.json()["user"]["must_change_password"] is True


def test_build_settings_ignores_the_environment(mongo_uri: str) -> None:
    assert build_settings(mongo_uri).bootstrap.email is None
