"""Helpers for the admin tests: an admin account, and the audit trail as admins read it."""

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.users.models import User
from tests.helpers import Account, login, register


async def admin_account(client: AsyncClient) -> Account:
    """A registered user promoted to admin, signed in again for a fresh token."""
    account = await register(client, name="Admin")
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"role": "admin"}}
    )
    token = (await login(client, account.email)).json()["access_token"]
    return Account(account.email, account.password, token, account.user_id)


async def audit_trail(client: AsyncClient, admin: Account, **filters: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/v1/admin/audit-logs",
        headers=admin.headers,
        # What this admin did: the trail is shared by every test.
        params={"page_size": 100, "actor_id": admin.user_id, **filters},
    )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])
