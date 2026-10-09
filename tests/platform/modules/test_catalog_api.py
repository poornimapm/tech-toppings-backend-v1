"""``/v1/modules``: what each user sees, and the preferences they control."""

from __future__ import annotations

from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.modules.models import ModuleOverride, UserModule
from tests.helpers import Account, FrozenClock, register

UPCOMING = {"expenses", "medicine", "receipts", "study", "gov_docs", "meals", "habits", "journal"}


async def modules(client: AsyncClient, account: Account, query: str = "") -> list[dict[str, Any]]:
    response = await client.get(f"/v1/modules?page_size=100{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def keys(items: list[dict[str, Any]]) -> list[str]:
    return [item["key"] for item in items]


async def add_override(key: str, **fields: Any) -> None:
    now = FrozenClock().now()
    await ModuleOverride(key=key, created_at=now, updated_at=now, **fields).insert()


# ------------------------------------------------------------------ listing


async def test_lists_every_module_in_default_order(client: AsyncClient) -> None:
    account = await register(client)

    items = await modules(client, account)

    assert keys(items)[:2] == ["notes", "expenses"]  # manifest order 5, then 10
    assert set(keys(items)) == UPCOMING | {"notes"}
    assert [item["position"] for item in items] == list(range(len(items)))
    notes = items[0]
    assert notes == {
        "key": "notes",
        "slug": "notes",
        "version": "1.0.0",
        "status": "live",
        "global_enabled": True,
        "enabled": True,
        "available": True,
        "pinned": False,
        "position": 0,
        "notify_requested": False,
        "has_settings": True,
        "stat": {"value": 0, "format": "number", "currency": None, "label": "stat_notes"},
    }
    upcoming = next(item for item in items if item["key"] == "gov_docs")
    assert upcoming["slug"] == "gov-docs"
    assert upcoming["available"] is False
    assert upcoming["stat"] is None


async def test_tile_stat_counts_only_the_callers_records(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner = await register(client)
    other = await register(other_client)
    for text in ("a", "b"):
        await client.post("/v1/notes", headers=owner.headers, json={"text": text})
    await other_client.post("/v1/notes", headers=other.headers, json={"text": "c"})

    mine = await modules(client, owner)

    assert mine[0]["stat"]["value"] == 2


async def test_filters_sorts_and_paginates(client: AsyncClient) -> None:
    account = await register(client)
    await client.patch(
        "/v1/modules/journal/preferences", headers=account.headers, json={"pinned": True}
    )

    upcoming = await modules(client, account, "&status=coming_soon")
    pinned = await modules(client, account, "&pinned=true")
    by_key = await modules(client, account, "&sort=-key")
    first_page = await client.get("/v1/modules?page_size=3", headers=account.headers)
    bad_sort = await client.get("/v1/modules?sort=name", headers=account.headers)

    assert set(keys(upcoming)) == UPCOMING
    assert keys(pinned) == ["journal"]
    assert keys(by_key) == sorted(UPCOMING | {"notes"}, reverse=True)
    assert first_page.json()["total"] == len(UPCOMING) + 1
    assert first_page.json()["has_next"] is True
    assert len(first_page.json()["items"]) == 3
    assert bad_sort.status_code == 422


async def test_unknown_module_is_not_found(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.get("/v1/modules/teleporter", headers=account.headers)

    assert response.status_code == 404
    assert response.json()["code"] == "MODULE_NOT_FOUND"


# ------------------------------------------------------------------ preferences


async def test_pins_persist_and_belong_to_one_user(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    other = await register(other_client)

    pinned = await client.patch(
        "/v1/modules/medicine/preferences", headers=account.headers, json={"pinned": True}
    )
    unchanged = await client.patch(
        "/v1/modules/medicine/preferences", headers=account.headers, json={}
    )

    assert pinned.json()["pinned"] is True
    assert unchanged.json()["pinned"] is True
    assert (await client.get("/v1/modules/medicine", headers=account.headers)).json()["pinned"]
    other_view = await other_client.get("/v1/modules/medicine", headers=other.headers)
    assert other_view.json()["pinned"] is False


async def test_preferences_reject_unknown_fields(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"status": "live"}
    )

    assert response.status_code == 422


async def test_reorder_persists_and_keeps_the_rest_in_order(client: AsyncClient) -> None:
    account = await register(client)
    default = keys(await modules(client, account))

    response = await client.put(
        "/v1/modules/order", headers=account.headers, json={"keys": ["journal", "notes"]}
    )
    arranged = keys(await modules(client, account))

    assert response.status_code == 204
    rest = [key for key in default if key not in {"journal", "notes"}]
    assert arranged == ["journal", "notes", *rest]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"keys": ["notes", "teleporter"]}, 404, "MODULE_NOT_FOUND"),
        ({"keys": ["notes", "notes"]}, 422, "VALIDATION_ERROR"),
        ({"keys": []}, 422, "VALIDATION_ERROR"),
    ],
)
async def test_reorder_rejects_bad_lists(
    client: AsyncClient, body: dict[str, Any], status: int, code: str
) -> None:
    account = await register(client)

    response = await client.put("/v1/modules/order", headers=account.headers, json=body)

    assert response.status_code == status
    assert response.json()["code"] == code


async def test_each_user_has_one_preference_record_per_module(client: AsyncClient) -> None:
    account = await register(client)

    await client.patch(
        "/v1/modules/meals/preferences", headers=account.headers, json={"pinned": True}
    )
    await client.put("/v1/modules/order", headers=account.headers, json={"keys": ["meals"]})

    stored = await UserModule.find(
        {"user_id": PydanticObjectId(account.user_id), "module_key": "meals"}
    ).to_list()
    assert len(stored) == 1
    assert stored[0].pinned is True
    assert stored[0].position == 0
    assert stored[0].module_key == "meals"


# ------------------------------------------------------------------ availability gate


async def test_turning_a_module_off_blocks_its_routes_for_that_user_only(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    other = await register(other_client)

    off = await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": False}
    )
    blocked = await client.get("/v1/notes", headers=account.headers)
    others = await other_client.get("/v1/notes", headers=other.headers)
    await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": True}
    )
    back = await client.get("/v1/notes", headers=account.headers)

    assert off.json()["available"] is False
    assert off.json()["stat"] is None
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "MODULE_DISABLED"
    assert others.status_code == 200
    assert back.status_code == 200


@pytest.mark.usefixtures("clean_overrides")
async def test_admin_switch_blocks_everyone_but_keeps_admin_routes(client: AsyncClient) -> None:
    account = await register(client)
    await add_override("notes", global_enabled=False)

    view = (await client.get("/v1/modules/notes", headers=account.headers)).json()
    blocked = await client.post("/v1/notes", headers=account.headers, json={"text": "x"})
    admin_route = await client.get("/v1/admin/notes", headers=account.headers)

    assert view["global_enabled"] is False
    assert view["enabled"] is True
    assert view["available"] is False
    assert blocked.status_code == 403
    assert admin_route.json()["code"] == "FORBIDDEN"  # role check, not the module gate


@pytest.mark.usefixtures("clean_overrides")
async def test_admin_overrides_status_and_order(client: AsyncClient) -> None:
    account = await register(client)
    await add_override("notes", status="coming_soon", order=99)
    await add_override("journal", order=0)

    items = await modules(client, account)
    blocked = await client.get("/v1/notes", headers=account.headers)

    assert keys(items)[0] == "journal"
    assert keys(items)[-1] == "notes"
    assert items[-1]["status"] == "coming_soon"
    assert blocked.status_code == 403


# ------------------------------------------------------------------ notify me


async def test_notify_me_is_idempotent_and_can_be_cancelled(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    owner = PydanticObjectId(account.user_id)

    first = await client.post("/v1/modules/receipts/interest", headers=account.headers)
    requested_at = await UserModule.find_one({"user_id": owner, "module_key": "receipts"})
    clock.advance(minutes=5)
    again = await client.post("/v1/modules/receipts/interest", headers=account.headers)
    kept = await UserModule.find_one({"user_id": owner, "module_key": "receipts"})
    cancelled = await client.delete("/v1/modules/receipts/interest", headers=account.headers)
    cancelled_again = await client.delete("/v1/modules/receipts/interest", headers=account.headers)

    assert first.json()["notify_requested"] is True
    assert again.json()["notify_requested"] is True
    assert requested_at is not None
    assert kept is not None
    assert kept.notify_requested_at == requested_at.notify_requested_at  # first request's time
    assert cancelled.json()["notify_requested"] is False
    assert cancelled_again.status_code == 200


async def test_notify_me_is_only_for_upcoming_modules(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.post("/v1/modules/notes/interest", headers=account.headers)

    assert response.status_code == 409
    assert response.json()["code"] == "MODULE_NOT_UPCOMING"


# ------------------------------------------------------------------ settings


async def test_settings_schema_defaults_and_save(client: AsyncClient) -> None:
    account = await register(client)
    headers = account.headers

    schema = (await client.get("/v1/modules/notes/settings/schema", headers=headers)).json()
    defaults = (await client.get("/v1/modules/notes/settings", headers=headers)).json()
    saved = await client.put(
        "/v1/modules/notes/settings", headers=headers, json={"default_sort": "oldest"}
    )
    reread = (await client.get("/v1/modules/notes/settings", headers=headers)).json()

    assert set(schema["properties"]) == {"default_sort", "preview_length"}
    assert defaults == {"default_sort": "newest", "preview_length": 80}
    assert saved.json() == {"default_sort": "oldest", "preview_length": 80}
    assert reread == saved.json()


@pytest.mark.parametrize(
    "body", [{"preview_length": 5000}, {"default_sort": "random"}, {"colour": "red"}]
)
async def test_invalid_settings_are_rejected_without_echoing_values(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)

    response = await client.put("/v1/modules/notes/settings", headers=account.headers, json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "MODULE_SETTINGS_INVALID"
    details = response.json()["details"]
    assert all(set(item) == {"loc", "msg", "type"} for item in details)  # no "input" echo
    assert all(str(value) not in item["msg"] for item in details for value in body.values())


async def test_modules_without_settings_say_so(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.get("/v1/modules/journal/settings", headers=account.headers)

    assert response.status_code == 404
    assert response.json()["code"] == "MODULE_NO_SETTINGS"


async def test_outdated_stored_settings_fall_back_to_defaults(client: AsyncClient) -> None:
    account = await register(client)
    await client.put(
        "/v1/modules/notes/settings", headers=account.headers, json={"default_sort": "oldest"}
    )
    # Simulate a newer module version that tightened one field and dropped another.
    await UserModule.get_pymongo_collection().update_one(
        {"user_id": PydanticObjectId(account.user_id), "module_key": "notes"},
        {"$set": {"settings": {"default_sort": "oldest", "preview_length": 9999, "legacy": 1}}},
    )

    response = await client.get("/v1/modules/notes/settings", headers=account.headers)

    assert response.json() == {"default_sort": "oldest", "preview_length": 80}
