"""How the route matrix (tests/isolation) makes one of this module's records for a user, and
checks afterwards that nobody else changed it: for every route under ``PREFIX`` that takes an
id. The matrix gives its accounts the modules only some accounts may use (member()), so each
route's own owner check is what answers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from tests.helpers import Account

PREFIX = "/v1/labor/"


async def create_project(client: AsyncClient, owner: Account) -> str:
    response = await client.post(f"{PREFIX}projects", headers=owner.headers, json={"name": "mine"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_project(client: AsyncClient, owner: Account, project_id: str) -> None:
    response = await client.get(f"{PREFIX}projects/{project_id}", headers=owner.headers)
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["status"]) == ("mine", "open")


async def create_worker(client: AsyncClient, owner: Account) -> str:
    project_id = await create_project(client, owner)
    response = await client.post(
        f"{PREFIX}projects/{project_id}/workers", headers=owner.headers, json={"name": "mine"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_worker(client: AsyncClient, owner: Account, worker_id: str) -> None:
    response = await client.get(f"{PREFIX}workers/{worker_id}", headers=owner.headers)
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["active"]) == ("mine", True)


async def create_payment(client: AsyncClient, owner: Account) -> str:
    worker_id = await create_worker(client, owner)
    worker = (await client.get(f"{PREFIX}workers/{worker_id}", headers=owner.headers)).json()
    response = await client.post(
        f"{PREFIX}projects/{worker['project_id']}/payments",
        headers=owner.headers,
        json={"worker_id": worker_id, "amount_minor": 100},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_payment(client: AsyncClient, owner: Account, payment_id: str) -> None:
    found = (await client.get(f"{PREFIX}audit?entity=payment", headers=owner.headers)).json()
    entries = [entry for entry in found["items"] if entry["entity_id"] == payment_id]
    assert [entry["action"] for entry in entries] == ["paid"]  # never voided by anyone else


NOBODY = "000000000000000000000000"
DAY = "?day=2026-10-01"
# Valid bodies and queries for routes that would otherwise fail validation before the lookup.
ROUTE_BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("PATCH", f"{PREFIX}projects/{{project_id}}"): {"name": "hijacked"},
    ("POST", f"{PREFIX}projects/{{project_id}}/close"): {},
    ("POST", f"{PREFIX}projects/{{project_id}}/reopen"): {},
    ("PUT", f"{PREFIX}projects/{{project_id}}/rates"): {"by_gender": False},
    ("POST", f"{PREFIX}projects/{{project_id}}/workers"): {"name": "hijacked"},
    ("PUT", f"{PREFIX}projects/{{project_id}}/attendance"): {
        "marks": [{"worker_id": NOBODY, "status": "full"}]
    },
    ("POST", f"{PREFIX}projects/{{project_id}}/payments"): {
        "worker_id": NOBODY,
        "amount_minor": 100,
    },
    ("POST", f"{PREFIX}projects/{{project_id}}/attendance/read"): {"text": "everyone full day"},
    ("PATCH", f"{PREFIX}workers/{{worker_id}}"): {"name": "hijacked"},
    ("PUT", f"{PREFIX}workers/{{worker_id}}/wage"): {"daily_wage_minor": 1},
    ("PUT", f"{PREFIX}workers/{{worker_id}}/active"): {"active": False},
    ("POST", f"{PREFIX}payments/{{payment_id}}/void"): {"reason": "hijacked"},
}
ROUTE_QUERIES: dict[tuple[str, str], str] = {
    ("GET", f"{PREFIX}projects/{{project_id}}/attendance"): DAY,
    ("PUT", f"{PREFIX}projects/{{project_id}}/attendance"): DAY,
    ("GET", f"{PREFIX}projects/{{project_id}}/report"): "?from=2026-10-01&to=2026-10-07",
    ("GET", f"{PREFIX}projects/{{project_id}}/report.csv"): "?from=2026-10-01&to=2026-10-07",
    ("GET", f"{PREFIX}workers/{{worker_id}}/history"): "?from=2026-10-01&to=2026-10-07",
}

RESOURCES: dict[
    str,
    tuple[
        Callable[[AsyncClient, Account], Awaitable[str]],
        Callable[[AsyncClient, Account, str], Awaitable[None]],
    ],
] = {
    "project_id": (create_project, check_project),
    "worker_id": (create_worker, check_worker),
    "payment_id": (create_payment, check_payment),
}
