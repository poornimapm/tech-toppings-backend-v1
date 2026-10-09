"""Admin vs user behaviour, exercised through the test-only notes resource:
``/v1/admin/notes`` uses ``AdminUser`` and an unrestricted scope."""

from __future__ import annotations

from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.users.models import User
from tests.helpers import login, register


async def make_admin(client: AsyncClient) -> str:
    account = await register(client, name="Admin")
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"role": "admin"}}
    )
    # Role is read from the database on every request, but sign in again for a fresh token.
    return str((await login(client, account.email)).json()["access_token"])


async def test_admin_sees_every_users_records(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    first = await register(other_client, name="First")
    note = await other_client.post("/v1/notes", headers=first.headers, json={"text": "a"})
    admin_token = await make_admin(client)

    response = await client.get(
        "/v1/admin/notes?page_size=100", headers={"Authorization": f"Bearer {admin_token}"}
    )

    assert response.status_code == 200
    assert note.json()["id"] in {item["id"] for item in response.json()["items"]}


async def test_regular_users_are_forbidden_from_admin_routes(client: AsyncClient) -> None:
    user = await register(client)

    response = await client.get("/v1/admin/notes", headers=user.headers)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_admin_routes_require_authentication(client: AsyncClient) -> None:
    response = await client.get("/v1/admin/notes")

    assert response.status_code == 401


async def test_demoting_an_admin_takes_effect_immediately(client: AsyncClient) -> None:
    token = await make_admin(client)
    me = (await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(me["id"])}, {"$set": {"role": "user"}}
    )

    response = await client.get("/v1/admin/notes", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
