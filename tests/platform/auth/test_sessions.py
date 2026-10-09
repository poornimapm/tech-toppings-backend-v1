from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.helpers import CSRF, Account, login, register


async def sessions(client: AsyncClient, account: Account, query: str = "") -> dict[str, Any]:
    response = await client.get(f"/v1/me/sessions{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_lists_my_devices_and_marks_the_current_one(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    await login(other_client, account.email)

    page = await sessions(client, account)

    items = page["items"]
    assert page["total"] == 2
    assert [item["current"] for item in items].count(True) == 1
    assert all(item["user_agent"] for item in items)


async def test_sessions_are_paginated_and_sortable(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    for _ in range(2):
        await login(other_client, account.email)

    first = await sessions(client, account, "?page_size=2&sort=created_at")
    second = await sessions(client, account, "?page=2&page_size=2&sort=created_at")

    assert (first["total"], first["has_next"], len(first["items"])) == (3, True, 2)
    assert (second["has_next"], len(second["items"])) == (False, 1)


async def test_unknown_sort_field_is_rejected(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.get("/v1/me/sessions?sort=-token_hash", headers=account.headers)

    assert response.status_code == 422
    assert response.json()["details"] == {
        "field": "token_hash",
        "allowed": ["created_at", "last_used_at"],
    }


async def test_revoking_a_device_signs_it_out(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    await login(other_client, account.email)
    page = await sessions(client, account)
    other = next(item for item in page["items"] if not item["current"])

    revoked = await client.delete(f"/v1/me/sessions/{other['id']}", headers=account.headers)
    again = await client.delete(f"/v1/me/sessions/{other['id']}", headers=account.headers)

    assert revoked.status_code == 204
    assert again.status_code == 204  # idempotent
    assert (await other_client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401
    assert (await sessions(client, account))["total"] == 1
    assert (await sessions(client, account, "?active=false"))["total"] == 2


async def test_unknown_or_malformed_session_ids_are_not_found(client: AsyncClient) -> None:
    account = await register(client)

    for session_id in ("000000000000000000000000", "not-an-object-id"):
        response = await client.delete(f"/v1/me/sessions/{session_id}", headers=account.headers)
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"
