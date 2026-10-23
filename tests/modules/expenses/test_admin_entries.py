"""``/v1/admin/expenses/entries``: an admin reads anyone's entries and corrects one. The
correction is a revision by the admin (source ``admin``) in the owner's data, plus an audit
entry naming the fields and the reason, never their values."""

from __future__ import annotations

from beanie import PydanticObjectId
from httpx import AsyncClient

from app.modules.expenses.features.entries.models import EntryRevision
from tests.helpers import register
from tests.modules.expenses.conftest import ENTRIES, add, history
from tests.platform.admin.conftest import admin_account, audit_trail

ADMIN_ENTRIES = "/v1/admin/expenses/entries"


async def test_an_admin_reads_one_persons_entries(client: AsyncClient) -> None:
    admin = await admin_account(client)
    asha, bob = await register(client), await register(client, name="Bob")
    tea = await add(client, asha, title="Tea")
    await add(client, bob, title="Bob's lunch")

    found = await client.get(ADMIN_ENTRIES, headers=admin.headers, params={"user_id": asha.user_id})
    assert found.status_code == 200, found.text
    assert [(item["id"], item["user_id"]) for item in found.json()["items"]] == [
        (tea["id"], asha.user_id)
    ]


async def test_a_correction_is_a_revision_by_the_admin_and_audited(client: AsyncClient) -> None:
    admin = await admin_account(client)
    asha = await register(client)
    tea = await add(client, asha, title="Tea", amount_minor=12000)

    corrected = await client.patch(
        f"{ADMIN_ENTRIES}/{tea['id']}",
        headers=admin.headers,
        json={"revision_no": tea["revision_no"], "reason": "Typo reported", "amount_minor": 1200},
    )
    assert corrected.status_code == 200, corrected.text
    assert (corrected.json()["amount_minor"], corrected.json()["title"]) == (1200, "Tea")

    mine = (await client.get(f"{ENTRIES}/{tea['id']}", headers=asha.headers)).json()
    assert mine["amount_minor"] == 1200  # the owner sees it
    latest = (await history(client, asha, tea["id"]))[0]
    assert (latest["source"], latest["changes"][0]["field"]) == ("admin", "amount_minor")
    revision = await EntryRevision.find_one(
        {"entity_id": PydanticObjectId(tea["id"]), "revision_no": latest["revision_no"]}
    )
    assert revision is not None
    assert str(revision.changed_by) == admin.user_id

    trail = await audit_trail(client, admin, action="entry.admin_corrected")
    assert (trail[0]["owner_id"], trail[0]["entity_id"]) == (asha.user_id, tea["id"])
    assert trail[0]["details"] == {
        "fields": ["amount_minor"],
        "revision_no": latest["revision_no"],
        "reason": "Typo reported",
    }


async def test_a_stale_or_unchanged_correction_writes_nothing(client: AsyncClient) -> None:
    admin = await admin_account(client)
    asha = await register(client)
    tea = await add(client, asha, title="Tea")

    async def correct(revision_no: int, **fields: object) -> int:
        response = await client.patch(
            f"{ADMIN_ENTRIES}/{tea['id']}",
            headers=admin.headers,
            json={"revision_no": revision_no, "reason": "Checking", **fields},
        )
        return response.status_code

    assert await correct(tea["revision_no"], title="Tea") == 200  # the same: no revision
    assert len(await history(client, asha, tea["id"])) == 1
    assert await correct(tea["revision_no"], title="Green tea") == 200
    assert await correct(tea["revision_no"], title="Black tea") == 409  # moved on meanwhile
    assert len(await audit_trail(client, admin, action="entry.admin_corrected")) == 1


async def test_only_admins_reach_it(client: AsyncClient) -> None:
    asha = await register(client)
    tea = await add(client, asha)
    assert (await client.get(ADMIN_ENTRIES, headers=asha.headers)).status_code == 403
    patched = await client.patch(
        f"{ADMIN_ENTRIES}/{tea['id']}",
        headers=asha.headers,
        json={"revision_no": 1, "reason": "Mine anyway", "title": "X"},
    )
    assert patched.status_code == 403
