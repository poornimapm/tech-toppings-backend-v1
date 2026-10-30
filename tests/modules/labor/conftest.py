"""Helpers for the Labor tests: owners made as the platform admin makes them (an account with
Labor, a one-time password changed at the first sign-in), and a tiny JPEG."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.helpers import Account, actor_of, bearer, login, unique_email
from tests.platform.admin.conftest import admin_account
from tests.platform.agents.conftest import agent_client  # noqa: F401 - a fixture, used by name

LABOR = "/v1/labor"
JPEG = b"\xff\xd8\xff\xe0" + bytes(64)  # a JPEG by its first bytes
NEW_PASSWORD = "Owner-Test-Pass-2026"  # test-only, never a real one


@dataclass
class Owner:
    account: Account
    name: str
    today: dt.date


def day_of(owner: Owner) -> dt.date:
    """Today, in the owner's own time zone (attendance is recorded by their calendar)."""
    return owner.today


async def owner_account(client: AsyncClient, admin: Account, name: str) -> Account:
    """An account made by the admin with Labor, its one-time password changed."""
    email = unique_email("owner")
    made = await client.post(
        "/v1/admin/users",
        headers=admin.headers,
        json={"email": email, "name": name, "modules": ["labor"]},
    )
    assert made.status_code == 201, made.text
    one_time = made.json()["one_time_password"]
    first = await login(client, email, one_time)
    changed = await client.post(
        "/v1/auth/password/change",
        headers=bearer(first.json()["access_token"]),
        json={"current_password": one_time, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    return Account(email, NEW_PASSWORD, changed.json()["access_token"], made.json()["user"]["id"])


async def make_owner(client: AsyncClient, app: FastAPI, admin: Account, name: str) -> Owner:
    account = await owner_account(client, admin, name)
    return Owner(account, name, (await actor_of(app, account)).today())


@pytest.fixture
async def admin(client: AsyncClient) -> Account:
    return await admin_account(client)


@pytest.fixture
async def owner(client: AsyncClient, app: FastAPI, admin: Account) -> Owner:
    return await make_owner(client, app, admin, "Meena")


@pytest.fixture
async def other_owner(client: AsyncClient, app: FastAPI, admin: Account) -> Owner:
    return await make_owner(client, app, admin, "Kumar")
