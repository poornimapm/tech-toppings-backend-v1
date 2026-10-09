"""Brief §7.3: user A must never read, change or delete user B's data, on any endpoint.

Every ``/v1`` route that takes a resource id is enumerated from the running app. Each id
parameter needs a factory below that creates such a resource for a user; a route with an
unknown parameter fails ``test_every_id_route_is_covered``, so new endpoints can't skip this.

Shared catalog keys (module ``{key}``) are not owned, so another user may use the same key; for
those the guarantee is that the call only ever changes the caller's own state.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.helpers import CSRF, Account, api_operations, register

PATH_PARAM = re.compile(r"{(\w+)}")
WRITE_BODIES: dict[str, dict[str, object]] = {
    "PATCH": {"text": "hijacked"},
    "PUT": {"text": "hijacked"},
}
# Valid bodies for routes whose generic body above would fail validation before the lookup.
ROUTE_BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("PATCH", "/v1/modules/{key}/preferences"): {"pinned": False, "enabled": False},
    ("PUT", "/v1/modules/{key}/settings"): {"default_sort": "newest", "preview_length": 10},
}


def body_for(method: str, path: str) -> dict[str, object] | None:
    return ROUTE_BODIES.get((method, path), WRITE_BODIES.get(method))


@dataclass(frozen=True)
class IdRoute:
    method: str
    path: str
    param: str

    def url(self, resource_id: str) -> str:
        return self.path.replace(f"{{{self.param}}}", resource_id)


def id_routes(app: FastAPI) -> list[IdRoute]:
    routes: list[IdRoute] = []
    for method, path in api_operations(app):
        params = PATH_PARAM.findall(path)
        if not params:
            continue
        assert len(params) == 1, f"extend the matrix for multi-id route {path}"
        routes.append(IdRoute(method, path, params[0]))
    return routes


# ------------------------------------------------------------------ resource factories


async def create_note(client: AsyncClient, owner: Account) -> str:
    response = await client.post("/v1/notes", headers=owner.headers, json={"text": "mine"})
    assert response.status_code == 201
    return str(response.json()["id"])


async def check_note(client: AsyncClient, owner: Account, note_id: str) -> None:
    response = await client.get(f"/v1/notes/{note_id}", headers=owner.headers)
    assert response.status_code == 200
    assert response.json()["text"] == "mine"


async def current_session(client: AsyncClient, owner: Account) -> str:
    page = (await client.get("/v1/me/sessions", headers=owner.headers)).json()
    return str(next(item["id"] for item in page["items"] if item["current"]))


async def check_session(client: AsyncClient, _owner: Account, _session_id: str) -> None:
    assert (await client.post("/v1/auth/refresh", headers=CSRF)).status_code == 200


Factory = Callable[[AsyncClient, Account], Awaitable[str]]
Checker = Callable[[AsyncClient, Account, str], Awaitable[None]]

RESOURCES: dict[str, tuple[Factory, Checker]] = {
    "note_id": (create_note, check_note),
    "session_id": (current_session, check_session),
}


async def owner_module_state(client: AsyncClient, owner: Account) -> str:
    """The owner customises the test module: pinned, first, custom settings."""
    headers = owner.headers
    await client.patch("/v1/modules/notes/preferences", headers=headers, json={"pinned": True})
    await client.put("/v1/modules/order", headers=headers, json={"keys": ["notes"]})
    await client.put("/v1/modules/notes/settings", headers=headers, json={"default_sort": "oldest"})
    return "notes"


async def snapshot_module(client: AsyncClient, owner: Account, key: str) -> dict[str, object]:
    view = (await client.get(f"/v1/modules/{key}", headers=owner.headers)).json()
    settings = (await client.get(f"/v1/modules/{key}/settings", headers=owner.headers)).json()
    return {"view": view, "settings": settings}


CATALOG: dict[str, Factory] = {"key": owner_module_state}


# ------------------------------------------------------------------ tests


def test_every_id_route_is_covered(app: FastAPI) -> None:
    routes = id_routes(app)

    assert routes, "expected at least one id route"
    uncovered = sorted({r.param for r in routes} - set(RESOURCES) - set(CATALOG))
    assert uncovered == [], f"add factories for: {uncovered}"


async def test_another_user_gets_not_found_on_every_id_route(
    app: FastAPI, client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await register(client, name="Owner")
    intruder = await register(other_client, name="Intruder")
    checked: list[str] = []

    for route in id_routes(app):
        if route.param not in RESOURCES:
            continue
        create, verify = RESOURCES[route.param]
        resource_id = await create(client, owner)

        response = await other_client.request(
            route.method,
            route.url(resource_id),
            headers=intruder.headers,
            json=body_for(route.method, route.path),
        )

        assert response.status_code == 404, f"{route.method} {route.path}: {response.text}"
        assert response.json()["code"] == "NOT_FOUND"
        await verify(client, owner, resource_id)  # the owner's data is untouched
        checked.append(f"{route.method} {route.path}")

    assert len(checked) == len([r for r in id_routes(app) if r.param in RESOURCES])


async def test_catalog_routes_only_change_the_callers_own_state(
    app: FastAPI, client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await register(client, name="Owner")
    intruder = await register(other_client, name="Intruder")
    routes = [r for r in id_routes(app) if r.param in CATALOG]
    assert routes, "expected catalog routes"

    for route in routes:
        key = await CATALOG[route.param](client, owner)
        before = await snapshot_module(client, owner, key)

        response = await other_client.request(
            route.method,
            route.url(key),
            headers=intruder.headers,
            json=body_for(route.method, route.path),
        )

        assert response.status_code < 500, f"{route.method} {route.path}: {response.text}"
        assert await snapshot_module(client, owner, key) == before, f"{route.method} {route.path}"


@pytest.mark.parametrize("bogus_id", ["000000000000000000000000", "nope"])
async def test_unknown_ids_are_not_found_for_their_owner_too(
    app: FastAPI, client: AsyncClient, bogus_id: str
) -> None:
    owner = await register(client)

    for route in id_routes(app):
        response = await client.request(
            route.method,
            route.url(bogus_id),
            headers=owner.headers,
            json=body_for(route.method, route.path),
        )
        assert response.status_code == 404, f"{route.method} {route.path}"


async def test_lists_only_ever_contain_the_callers_records(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await register(client)
    intruder = await register(other_client)
    await create_note(client, owner)
    await create_note(other_client, intruder)

    mine = (await client.get("/v1/notes", headers=owner.headers)).json()
    sessions = (await client.get("/v1/me/sessions", headers=owner.headers)).json()

    assert {item["user_id"] for item in mine["items"]} == {owner.user_id}
    assert sessions["total"] == 1
