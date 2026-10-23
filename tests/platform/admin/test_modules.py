"""``/v1/admin/modules``: each module as its manifest says and as an admin overrides it
(status, the global switch, order), null following the manifest again, each change audited."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers import register
from tests.platform.admin.conftest import admin_account, audit_trail

MODULES = "/v1/admin/modules"
pytestmark = pytest.mark.usefixtures("clean_overrides")


async def notes_of(client: AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    found = (await client.get(MODULES, headers=headers)).json()
    return next(item for item in found if item["key"] == "notes")


async def test_lists_every_module_as_its_manifest_says(client: AsyncClient) -> None:
    admin = await admin_account(client)
    assert await notes_of(client, admin.headers) == {
        "key": "notes",
        "manifest_status": "live",
        "manifest_order": 5,
        "status": "live",
        "global_enabled": True,
        "order": 5,
        "overridden": [],
    }


async def test_an_override_reaches_users_and_null_follows_the_manifest(client: AsyncClient) -> None:
    admin = await admin_account(client)
    user = await register(client)

    soon = await client.patch(
        f"{MODULES}/notes", headers=admin.headers, json={"status": "coming_soon", "order": 2}
    )
    assert soon.status_code == 200, soon.text
    assert (soon.json()["status"], soon.json()["order"], soon.json()["overridden"]) == (
        "coming_soon",
        2,
        ["status", "order"],
    )
    seen = (await client.get("/v1/modules/notes", headers=user.headers)).json()
    assert seen["status"] == "coming_soon"

    back = await client.patch(f"{MODULES}/notes", headers=admin.headers, json={"status": None})
    assert (back.json()["status"], back.json()["overridden"]) == ("live", ["order"])

    trail = await audit_trail(client, admin, entity="module")
    assert [item["details"] for item in trail] == [
        {
            "status": {"from": "coming_soon", "to": "live"},
            "overridden": {"from": ["status", "order"], "to": ["order"]},
        },
        {
            "status": {"from": "live", "to": "coming_soon"},
            "order": {"from": 5, "to": 2},
            "overridden": {"from": [], "to": ["status", "order"]},
        },
    ]


async def test_pinning_to_what_the_manifest_says_is_audited_too(client: AsyncClient) -> None:
    """Nothing users see changes yet, but the next release's manifest won't reach it."""
    admin = await admin_account(client)
    pinned = await client.patch(f"{MODULES}/notes", headers=admin.headers, json={"order": 5})
    assert (pinned.json()["order"], pinned.json()["overridden"]) == (5, ["order"])

    trail = await audit_trail(client, admin, entity="module")
    assert [item["details"] for item in trail] == [{"overridden": {"from": [], "to": ["order"]}}]


async def test_switched_off_everywhere_it_answers_no_one(client: AsyncClient) -> None:
    admin = await admin_account(client)
    user = await register(client)
    off = await client.patch(
        f"{MODULES}/notes", headers=admin.headers, json={"global_enabled": False}
    )
    assert off.json()["global_enabled"] is False
    refused = await client.get("/v1/notes", headers=user.headers)
    assert (refused.status_code, refused.json()["code"]) == (403, "MODULE_DISABLED")

    same = await client.patch(
        f"{MODULES}/notes", headers=admin.headers, json={"global_enabled": False}
    )
    assert same.status_code == 200
    assert len(await audit_trail(client, admin, entity="module")) == 1  # nothing changed again


async def test_an_unknown_module_is_not_found(client: AsyncClient) -> None:
    admin = await admin_account(client)
    response = await client.patch(f"{MODULES}/nope", headers=admin.headers, json={"order": 1})
    assert response.status_code == 404
