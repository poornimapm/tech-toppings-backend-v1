"""Brief §7.3: user A must never read, change or delete user B's data, on any endpoint.

Every ``/v1`` route that takes a resource id is enumerated from the running app. Each id
parameter needs a factory below that creates such a resource for a user; a route with an
unknown parameter fails ``test_every_id_route_is_covered``, so new endpoints can't skip this.
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
    response = await client.post("/v1/test-notes", headers=owner.headers, json={"text": "mine"})
    assert response.status_code == 201
    return str(response.json()["id"])


async def check_note(client: AsyncClient, owner: Account, note_id: str) -> None:
    response = await client.get(f"/v1/test-notes/{note_id}", headers=owner.headers)
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


# ------------------------------------------------------------------ tests


def test_every_id_route_is_covered(app: FastAPI) -> None:
    routes = id_routes(app)

    assert routes, "expected at least one id route"
    uncovered = sorted({r.param for r in routes} - set(RESOURCES))
    assert uncovered == [], f"add factories for: {uncovered}"


async def test_another_user_gets_not_found_on_every_id_route(
    app: FastAPI, client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await register(client, name="Owner")
    intruder = await register(other_client, name="Intruder")
    checked: list[str] = []

    for route in id_routes(app):
        create, verify = RESOURCES[route.param]
        resource_id = await create(client, owner)

        response = await other_client.request(
            route.method,
            route.url(resource_id),
            headers=intruder.headers,
            json=WRITE_BODIES.get(route.method),
        )

        assert response.status_code == 404, f"{route.method} {route.path}: {response.text}"
        assert response.json()["code"] == "NOT_FOUND"
        await verify(client, owner, resource_id)  # the owner's data is untouched
        checked.append(f"{route.method} {route.path}")

    assert len(checked) == len(id_routes(app))


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
            json=WRITE_BODIES.get(route.method),
        )
        assert response.status_code == 404, f"{route.method} {route.path}"


async def test_lists_only_ever_contain_the_callers_records(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await register(client)
    intruder = await register(other_client)
    await create_note(client, owner)
    await create_note(other_client, intruder)

    mine = (await client.get("/v1/test-notes", headers=owner.headers)).json()
    sessions = (await client.get("/v1/me/sessions", headers=owner.headers)).json()

    assert {item["user_id"] for item in mine["items"]} == {owner.user_id}
    assert sessions["total"] == 1
