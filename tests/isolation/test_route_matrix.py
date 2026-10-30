"""Brief §7.3: user A must never read, change or delete user B's data, on any endpoint.

Every ``/v1`` route that takes a resource id is enumerated from the running app. Each id
parameter needs a factory below that creates such a resource for a user, or one in its module's
``tests/modules/<key>/isolation.py``; a route with an unknown parameter fails
``test_every_id_route_is_covered``, so new endpoints can't skip this.

Shared catalog keys (module ``{key}``) are not owned, so another user may use the same key; for
those the guarantee is that the call only ever changes the caller's own state.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.repository import Scope
from app.platform.context import ActorContext
from app.platform.modules.repository import UserModuleRepository
from app.platform.notifications.inbox import Inbox
from app.platform.reminders.service import Reminders
from app.platform.users.models import User
from tests import modules as modules_tests
from tests.helpers import CSRF, Account, FrozenClock, api_operations, build_settings, register

PATH_PARAM = re.compile(r"{(\w+)}")
# Admin routes reach every user's data by design; tests/architecture/test_access_rules.py
# checks that each one answers 403 to anyone but an admin (and 401 to nobody signed in).
ADMIN_PREFIX = "/v1/admin/"
RULE_DUE_ON = "2030-01-05"  # the owner's bill's first due date: payable by them, not by others
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
    ("PATCH", "/v1/expenses/products/{product_id}"): {"name": "hijacked"},
    ("POST", "/v1/expenses/products/{product_id}/merge"): {"product_id": "0" * 24},
    ("POST", "/v1/expenses/products/{product_id}/apart"): {"product_id": "0" * 24},
    ("PATCH", "/v1/expenses/budgets/{budget_id}"): {"limit_minor": 1},
    ("PATCH", "/v1/expenses/goals/{goal_id}"): {"name": "hijacked", "revision_no": 1},
    ("POST", "/v1/expenses/goals/{goal_id}/contributions"): {
        "amount_minor": 1,
        "date": "2026-10-08",
    },
    ("PATCH", "/v1/expenses/ious/{iou_id}"): {"counterparty": "hijacked", "revision_no": 1},
    ("POST", "/v1/expenses/ious/{iou_id}/settlements"): {"amount_minor": 1, "date": "2026-10-08"},
    ("PATCH", "/v1/expenses/recurring/{rule_id}"): {"active": False},
    ("PATCH", "/v1/expenses/saved-filters/{saved_id}"): {"name": "hijacked"},
    ("POST", "/v1/expenses/recurring/{rule_id}/mark-paid"): {"due_on": RULE_DUE_ON},
    ("POST", "/v1/reminders/{reminder_id}/snooze"): {"days": 1},
}

# Query parameters a route needs before it looks anything up.
ROUTE_QUERIES: dict[tuple[str, str], str] = {
    ("POST", "/v1/agents/{name}/photo-runs"): "?skill=scan-receipt",
}


def body_for(method: str, path: str) -> dict[str, object] | None:
    return ROUTE_BODIES.get((method, path), WRITE_BODIES.get(method))


async def member(app: FastAPI, client: AsyncClient, name: str = "Asha") -> Account:
    """A new account given every module only some accounts may use (an admin would): their
    routes are reached, so it is each route's own owner check that answers, not the module
    being hidden."""
    account = await register(client, name=name)
    owner = Scope.for_user(PydanticObjectId(account.user_id))
    preferences = UserModuleRepository(owner, FrozenClock())
    for module in app.state.modules:
        if module.manifest.audience == "granted":
            await preferences.upsert(module.manifest.key, {"granted_at": FrozenClock().now()})
    return account


@dataclass(frozen=True)
class IdRoute:
    method: str
    path: str
    param: str

    def url(self, resource_id: str) -> str:
        query = ROUTE_QUERIES.get((self.method, self.path), "")
        return self.path.replace(f"{{{self.param}}}", resource_id) + query


def id_routes(app: FastAPI) -> list[IdRoute]:
    routes: list[IdRoute] = []
    for method, path in api_operations(app):
        params = PATH_PARAM.findall(path)
        if not params or path.startswith(ADMIN_PREFIX):
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


async def create_reminder(client: AsyncClient, owner: Account) -> str:
    """A reminder the notes module scheduled for next week (so it is not told yet)."""
    owner_id = PydanticObjectId(owner.user_id)
    user = await User.get(owner_id)
    assert user is not None
    settings = build_settings("mongodb://localhost/unused")
    ctx = ActorContext(user, Scope.for_user(owner_id), FrozenClock(), settings, state=None)
    note = await create_note(client, owner)
    week = ctx.today() + timedelta(days=7)
    await Reminders(ctx).schedule("notes.due", module="notes", ref_id=note, due_on=week, note=note)
    page = (await client.get("/v1/reminders", headers=owner.headers)).json()
    return str(next(item["id"] for item in page["items"] if item["params"]["note"] == note))


async def check_reminder(client: AsyncClient, owner: Account, reminder_id: str) -> None:
    page = (await client.get("/v1/reminders", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == reminder_id)
    assert (mine["sent"], mine["snoozed_until"]) == (False, None)  # still listed, as it was


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


async def create_product(client: AsyncClient, owner: Account) -> str:
    """Something bought by measure starts a product of its own."""
    await client.post(
        "/v1/expenses/entries",
        headers=owner.headers,
        json={
            "title": "Tomato",
            "amount_minor": 4000,
            "quantity": 1,
            "unit": "kg",
            "category_id": "groceries",
            "date": "2026-10-08",
        },
    )
    page = (await client.get("/v1/expenses/products?title=Tomato", headers=owner.headers)).json()
    return str(page["items"][0]["id"])


async def check_product(client: AsyncClient, owner: Account, product_id: str) -> None:
    product = (
        await client.get(f"/v1/expenses/products/{product_id}", headers=owner.headers)
    ).json()
    assert (product["name"], product["aliases"], product["apart"]) == ("Tomato", ["tomato"], [])
    assert (product["alert_above_minor"], product["price_unit"]) == (None, "kg")


async def create_budget(client: AsyncClient, owner: Account) -> str:
    response = await client.post(
        "/v1/expenses/budgets",
        headers=owner.headers,
        json={"name": "Mine", "scope": "overall", "period": "month", "limit_minor": 100000},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_budget(client: AsyncClient, owner: Account, budget_id: str) -> None:
    page = (await client.get("/v1/expenses/budgets", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == budget_id)
    assert (mine["name"], mine["limit_minor"]) == ("Mine", 100000)


async def create_goal(client: AsyncClient, owner: Account) -> str:
    response = await client.post(
        "/v1/expenses/goals",
        headers=owner.headers,
        json={"name": "Mine", "target_minor": 100000},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_goal(client: AsyncClient, owner: Account, goal_id: str) -> None:
    page = (await client.get("/v1/expenses/goals", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == goal_id)
    assert (mine["name"], mine["saved_minor"], mine["revision_no"]) == ("Mine", 0, 1)


async def create_iou(client: AsyncClient, owner: Account) -> str:
    response = await client.post(
        "/v1/expenses/ious",
        headers=owner.headers,
        json={"counterparty": "Ravi", "direction": "lent", "amount_minor": 50000},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_iou(client: AsyncClient, owner: Account, iou_id: str) -> None:
    page = (await client.get("/v1/expenses/ious?state=all", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == iou_id)
    assert (mine["counterparty"], mine["settled_minor"], mine["revision_no"]) == ("Ravi", 0, 1)


async def create_rule(client: AsyncClient, owner: Account) -> str:
    """A monthly bill, first due years ahead: nothing is added or told yet."""
    response = await client.post(
        "/v1/expenses/recurring",
        headers=owner.headers,
        json={
            "template": {"title": "Rent", "amount_minor": 1500000, "category_id": "housing"},
            "cadence": {"unit": "month", "anchor": RULE_DUE_ON},
            "mode": "remind",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_rule(client: AsyncClient, owner: Account, rule_id: str) -> None:
    page = (await client.get("/v1/expenses/recurring", headers=owner.headers)).json()
    mine = next(item for item in page["items"] if item["id"] == rule_id)
    assert (mine["active"], mine["next_run"]) == (True, RULE_DUE_ON)  # not paused, not paid
    entries = (await client.get("/v1/expenses/entries", headers=owner.headers)).json()
    assert all(item["recurring_id"] != rule_id for item in entries["items"])


async def create_saved_filter(client: AsyncClient, owner: Account) -> str:
    response = await client.post(
        "/v1/expenses/saved-filters",
        headers=owner.headers,
        json={"name": f"Mine {uuid.uuid4().hex[:8]}", "view": {"show": "expense", "tag": "goa"}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_saved_filter(client: AsyncClient, owner: Account, saved_id: str) -> None:
    views = (await client.get("/v1/expenses/saved-filters", headers=owner.headers)).json()
    mine = next(item for item in views if item["id"] == saved_id)
    assert (mine["name"].startswith("Mine "), mine["view"]["tag"]) == (True, "goa")


Factory = Callable[[AsyncClient, Account], Awaitable[str]]
Checker = Callable[[AsyncClient, Account, str], Awaitable[None]]

RESOURCES: dict[str, tuple[Factory, Checker]] = {
    "note_id": (create_note, check_note),
    "session_id": (current_session, check_session),
    "run_id": (create_run, check_run),
    "proposal_id": (create_proposal, check_proposal),
    "notification_id": (create_notification, check_notification),
    "reminder_id": (create_reminder, check_reminder),
    "entry_id": (create_entry, check_entry),
    "category_id": (create_category, check_category),
    "product_id": (create_product, check_product),
    "budget_id": (create_budget, check_budget),
    "goal_id": (create_goal, check_goal),
    "iou_id": (create_iou, check_iou),
    "rule_id": (create_rule, check_rule),
    "saved_id": (create_saved_filter, check_saved_filter),
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


def module_resources() -> dict[str, dict[str, tuple[Factory, Checker]]]:
    """A module's own factories, by the prefix of its routes: ``tests/modules/<key>/isolation.py``
    defines ``PREFIX`` and ``RESOURCES``, as the module generator writes it (ADR-0020). Two
    modules may then name their ids alike (``item_id``)."""
    found: dict[str, dict[str, tuple[Factory, Checker]]] = {}
    for info in pkgutil.iter_modules(modules_tests.__path__):
        if not info.ispkg:  # a shared conftest or helper, not a module's tests
            continue
        name = f"{modules_tests.__name__}.{info.name}.isolation"
        if importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        found[module.PREFIX] = module.RESOURCES
        # Valid bodies and queries for its routes that would fail validation before the lookup.
        ROUTE_BODIES.update(getattr(module, "ROUTE_BODIES", {}))
        ROUTE_QUERIES.update(getattr(module, "ROUTE_QUERIES", {}))
    return found


MODULE_RESOURCES = module_resources()


def resource_for(route: IdRoute) -> tuple[Factory, Checker] | None:
    """How to make and check what the route's id names: the module's own way, else the shared."""
    for prefix, resources in MODULE_RESOURCES.items():
        if route.path.startswith(prefix) and route.param in resources:
            return resources[route.param]
    return RESOURCES.get(route.param)


# ------------------------------------------------------------------ tests


def test_every_id_route_is_covered(app: FastAPI) -> None:
    routes = id_routes(app)

    assert routes, "expected at least one id route"
    uncovered = sorted(
        {f"{r.param} ({r.path})" for r in routes if not resource_for(r) and r.param not in CATALOG}
    )
    assert uncovered == [], f"add factories for: {uncovered}"


async def test_another_user_gets_not_found_on_every_id_route(
    app: FastAPI, client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await member(app, client, name="Owner")
    intruder = await member(app, other_client, name="Intruder")
    checked: list[str] = []

    for route in id_routes(app):
        factories = resource_for(route)
        if factories is None:
            continue
        create, verify = factories
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

    assert len(checked) == len([r for r in id_routes(app) if resource_for(r)])


async def test_catalog_routes_only_change_the_callers_own_state(
    app: FastAPI, client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await member(app, client, name="Owner")
    intruder = await member(app, other_client, name="Intruder")
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
    owner = await member(app, client)

    for route in id_routes(app):
        response = await client.request(
            route.method,
            route.url(bogus_id),
            headers=owner.headers,
            json=body_for(route.method, route.path),
        )
        assert response.status_code == 404, f"{route.method} {route.path}"


async def test_lists_only_ever_contain_the_callers_records(
    app: FastAPI, client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await member(app, client)
    intruder = await member(app, other_client)
    await create_note(client, owner)
    await create_note(other_client, intruder)

    mine = (await client.get("/v1/notes", headers=owner.headers)).json()
    sessions = (await client.get("/v1/me/sessions", headers=owner.headers)).json()

    assert {item["user_id"] for item in mine["items"]} == {owner.user_id}
    assert sessions["total"] == 1
