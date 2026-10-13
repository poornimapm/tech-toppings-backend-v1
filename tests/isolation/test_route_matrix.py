"""Brief §7.3: user A must never read, change or delete user B's data, on any endpoint.

Every ``/v1`` route that takes a resource id is enumerated from the running app. Each id
parameter needs a factory below that creates such a resource for a user; a route with an
unknown parameter fails ``test_every_id_route_is_covered``, so new endpoints can't skip this.

Shared catalog keys (module ``{key}``) are not owned, so another user may use the same key; for
those the guarantee is that the call only ever changes the caller's own state.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.repository import Scope
from app.platform.notifications.inbox import Inbox
from tests.helpers import CSRF, Account, FrozenClock, api_operations, build_settings, register

PATH_PARAM = re.compile(r"{(\w+)}")
WRITE_BODIES: dict[str, dict[str, object]] = {
    "PATCH": {"text": "hijacked"},
    "PUT": {"text": "hijacked"},
}
# Valid bodies for routes whose generic body above would fail validation before the lookup.
ROUTE_BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("PATCH", "/v1/modules/{key}/preferences"): {"pinned": False, "enabled": False},
    ("PUT", "/v1/modules/{key}/settings"): {"default_sort": "newest", "preview_length": 10},
    ("POST", "/v1/agents/{name}/runs"): {"input": "pin notes"},
    ("PUT", "/v1/expenses/entries/{entry_id}"): {
        "title": "hijacked",
        "amount_minor": 1,
        "category_id": "groceries",
        "date": "2026-10-08",
        "revision_no": 1,
    },
    ("POST", "/v1/expenses/entries/{entry_id}/restore"): {"revision_no": 1},
    ("PATCH", "/v1/expenses/categories/{category_id}"): {"name": "hijacked"},
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


async def agent_run(client: AsyncClient, owner: Account) -> dict[str, Any]:
    """A run that proposes a change (static mode: no AI needed in tests)."""
    response = await client.post(
        "/v1/agents/assistant/runs", headers=owner.headers, json={"input": "hide journal"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["proposals"], response.text
    return dict(response.json())


async def create_run(client: AsyncClient, owner: Account) -> str:
    return str((await agent_run(client, owner))["id"])


async def check_run(client: AsyncClient, owner: Account, run_id: str) -> None:
    response = await client.get(f"/v1/agents/runs/{run_id}", headers=owner.headers)
    assert response.status_code == 200
    assert response.json()["proposals"][0]["status"] == "pending"


async def create_proposal(client: AsyncClient, owner: Account) -> str:
    return str((await agent_run(client, owner))["proposals"][0]["id"])


async def check_proposal(client: AsyncClient, owner: Account, proposal_id: str) -> None:
    journal = (await client.get("/v1/modules/journal", headers=owner.headers)).json()
    assert journal["enabled"] is True  # the intruder's call never ran it
    rejected = await client.post(
        f"/v1/agents/proposals/{proposal_id}/reject", headers=owner.headers
    )
    assert rejected.json()["status"] == "rejected"  # and it is still the owner's to decide


async def create_notification(client: AsyncClient, owner: Account) -> str:
    inbox = Inbox(
        Scope.for_user(PydanticObjectId(owner.user_id)),
        FrozenClock(),
        build_settings("mongodb://localhost/unused"),
    )
    await inbox.notify("module.launched", module="notes")
    page = (await client.get("/v1/notifications?unread=true", headers=owner.headers)).json()
    return str(page["items"][0]["id"])


async def check_notification(client: AsyncClient, owner: Account, notification_id: str) -> None:
    page = (await client.get("/v1/notifications", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == notification_id)
    assert mine["read"] is False  # still there, and still unread


async def create_entry(client: AsyncClient, owner: Account) -> str:
    response = await client.post(
        "/v1/expenses/entries",
        headers=owner.headers,
        json={
            "title": "Rice",
            "amount_minor": 12500,
            "category_id": "groceries",
            "date": "2026-10-08",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_entry(client: AsyncClient, owner: Account, entry_id: str) -> None:
    entry = (await client.get(f"/v1/expenses/entries/{entry_id}", headers=owner.headers)).json()
    history = await client.get(f"/v1/expenses/entries/{entry_id}/revisions", headers=owner.headers)
    assert (entry["title"], entry["revision_no"], entry["deleted"]) == ("Rice", 1, False)
    assert history.json()["total"] == 1  # nobody else added to its history either


async def create_category(client: AsyncClient, owner: Account) -> str:
    response = await client.post(
        "/v1/expenses/categories",
        headers=owner.headers,
        json={"name": f"Pets {uuid.uuid4().hex[:6]}", "icon": "🐶", "accent": "teal"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_category(client: AsyncClient, owner: Account, category_id: str) -> None:
    page = (await client.get("/v1/expenses/categories", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == category_id)
    assert mine["name"].startswith("Pets ")
    assert mine["archived"] is False


Factory = Callable[[AsyncClient, Account], Awaitable[str]]
Checker = Callable[[AsyncClient, Account, str], Awaitable[None]]

RESOURCES: dict[str, tuple[Factory, Checker]] = {
    "note_id": (create_note, check_note),
    "session_id": (current_session, check_session),
    "run_id": (create_run, check_run),
    "proposal_id": (create_proposal, check_proposal),
    "notification_id": (create_notification, check_notification),
    "entry_id": (create_entry, check_entry),
    "category_id": (create_category, check_category),
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


async def owner_agent_state(client: AsyncClient, owner: Account) -> str:
    """The owner has a run with a pending proposal."""
    await create_run(client, owner)
    return "assistant"


async def snapshot_agent(client: AsyncClient, owner: Account, _name: str) -> dict[str, object]:
    modules = (await client.get("/v1/modules?page_size=100", headers=owner.headers)).json()
    agents = (await client.get("/v1/agents", headers=owner.headers)).json()
    return {"modules": modules, "agents": agents}


Snapshot = Callable[[AsyncClient, Account, str], Awaitable[dict[str, object]]]

CATALOG: dict[str, tuple[Factory, Snapshot]] = {
    "key": (owner_module_state, snapshot_module),
    "name": (owner_agent_state, snapshot_agent),
}


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
        prepare, snapshot = CATALOG[route.param]
        key = await prepare(client, owner)
        before = await snapshot(client, owner, key)

        response = await other_client.request(
            route.method,
            route.url(key),
            headers=intruder.headers,
            json=body_for(route.method, route.path),
        )

        assert response.status_code < 500, f"{route.method} {route.path}: {response.text}"
        assert await snapshot(client, owner, key) == before, f"{route.method} {route.path}"


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
