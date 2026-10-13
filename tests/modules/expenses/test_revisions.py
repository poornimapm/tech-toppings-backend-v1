"""History of an entry (ADR-0002): one revision per change, restore, conflicts, recovery after
a crash between the entry and its revision, and creating once per Idempotency-Key."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from beanie import PydanticObjectId
from httpx import AsyncClient

from app.modules.expenses.features.entries.models import Entry, EntryRevision
from tests.helpers import register
from tests.modules.expenses.conftest import (
    ENTRIES,
    KEY_HEADER,
    add,
    entry,
    history,
    listed,
    post,
    save,
)


async def test_every_change_writes_exactly_one_revision(client: AsyncClient) -> None:
    account = await register(client)
    url = f"{ENTRIES}/{{}}"
    first = await add(client, account)
    entry_url = url.format(first["id"])

    edited = (await save(client, account, first, title="Basmati rice", amount_minor=18000)).json()
    unchanged = (await save(client, account, edited)).json()  # saved without touching anything
    await client.delete(entry_url, headers=account.headers)
    back = (await client.post(f"{entry_url}/undelete", headers=account.headers)).json()
    back_again = (await client.post(f"{entry_url}/undelete", headers=account.headers)).json()
    first_version = {"revision_no": 1}
    restored = (
        await client.post(f"{entry_url}/restore", headers=account.headers, json=first_version)
    ).json()
    restored_again = (
        await client.post(f"{entry_url}/restore", headers=account.headers, json=first_version)
    ).json()

    revisions = await history(client, account, first["id"])
    assert [(item["revision_no"], item["action"]) for item in revisions] == [
        (5, "restore"),
        (4, "undelete"),
        (3, "delete"),
        (2, "update"),
        (1, "create"),
    ]
    saved = (edited, unchanged, back, back_again, restored, restored_again)
    assert [item["revision_no"] for item in saved] == [2, 2, 4, 4, 5, 5]  # no-ops add nothing
    assert revisions[3]["changes"] == [
        {"field": "title", "old": "Rice", "new": "Basmati rice"},
        {"field": "amount_minor", "old": 12500, "new": 18000},
    ]
    assert revisions[0]["changes"] == [
        {"field": "title", "old": "Basmati rice", "new": "Rice"},
        {"field": "amount_minor", "old": 18000, "new": 12500},
    ]
    # Restoring is a new revision whose fields equal the old one's; history is not rewritten.
    assert revisions[0]["snapshot"] == revisions[4]["snapshot"]
    assert (restored["title"], restored["amount_minor"]) == ("Rice", 12500)
    assert {item["source"] for item in revisions} == {"manual"}


async def test_restoring_needs_a_revision_of_a_live_entry(client: AsyncClient) -> None:
    account = await register(client)
    first = await add(client, account)
    url = f"{ENTRIES}/{first['id']}"

    missing = await client.post(f"{url}/restore", headers=account.headers, json={"revision_no": 9})
    await client.delete(url, headers=account.headers)
    deleted = await client.post(f"{url}/restore", headers=account.headers, json={"revision_no": 1})

    assert (missing.status_code, deleted.status_code) == (404, 404)
    assert len(await history(client, account, first["id"])) == 2  # create, delete: no more


async def test_a_stale_edit_is_refused_not_merged(client: AsyncClient) -> None:
    account = await register(client)
    first = await add(client, account)
    await save(client, account, first, title="Changed on my phone")

    stale = await save(client, account, first, title="Changed on my laptop")

    current = (await client.get(f"{ENTRIES}/{first['id']}", headers=account.headers)).json()
    assert stale.status_code == 409
    assert (stale.json()["code"], stale.json()["details"]) == ("VERSION_CONFLICT", {"current": 2})
    assert (current["title"], current["revision_no"]) == ("Changed on my phone", 2)


async def test_two_edits_at_once_cannot_both_win(client: AsyncClient) -> None:
    account = await register(client)
    first = await add(client, account)

    responses = await asyncio.gather(
        *(save(client, account, first, title=f"Edit {n}") for n in range(4))
    )

    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409]
    assert len(await history(client, account, first["id"])) == 2


async def test_a_crash_after_an_edit_loses_no_revision(
    client: AsyncClient, crash_before_revision: Callable[[], None]
) -> None:
    account = await register(client)
    first = await add(client, account)

    crash_before_revision()
    crashed = await save(client, account, first, title="Saved, then the server died")

    # The entry was changed, but its revision was never written ...
    assert crashed.status_code == 500
    assert await EntryRevision.find({"entity_id": PydanticObjectId(first["id"])}).count() == 1
    # ... until anyone reads the entry again: then the history is complete, exactly once.
    read = (await client.get(f"{ENTRIES}/{first['id']}", headers=account.headers)).json()
    await listed(client, account)
    revisions = await history(client, account, first["id"])
    assert (read["title"], read["revision_no"]) == ("Saved, then the server died", 2)
    assert [(item["revision_no"], item["action"]) for item in revisions] == [
        (2, "update"),
        (1, "create"),
    ]
    assert revisions[0]["changes"][0]["new"] == "Saved, then the server died"


async def test_the_next_edit_first_finishes_an_interrupted_one(
    client: AsyncClient, crash_before_revision: Callable[[], None]
) -> None:
    account = await register(client)
    first = await add(client, account)
    crash_before_revision()
    await save(client, account, first, title="Interrupted")

    # No read in between: the next write itself must not overwrite the parked revision.
    following = await save(client, account, first | {"revision_no": 2}, title="Next")

    assert following.status_code == 200
    assert [item["action"] for item in await history(client, account, first["id"])] == [
        "update",
        "update",
        "create",
    ]


async def test_a_revision_written_but_not_cleared_is_not_written_twice(
    client: AsyncClient,
) -> None:
    account = await register(client)
    first = await add(client, account)
    entry_id = PydanticObjectId(first["id"])
    written = await EntryRevision.find_one({"entity_id": entry_id})
    assert written is not None
    # As if the process died after inserting the revision but before clearing the parked copy.
    parked = written.model_dump(exclude={"id", "revision_id", "entity_id", "user_id"})
    await Entry.get_pymongo_collection().update_one(
        {"_id": entry_id}, {"$set": {"pending_revision": parked}}
    )

    listing = await listed(client, account)
    edited = await save(client, account, first, title="Still editable")

    stored = await Entry.get(entry_id)
    assert listing["total"] == 1
    assert edited.status_code == 200
    assert stored is not None
    assert stored.pending_revision is None
    assert [item["revision_no"] for item in await history(client, account, first["id"])] == [2, 1]


async def test_a_crash_while_adding_still_leaves_one_entry_with_its_history(
    client: AsyncClient, crash_before_revision: Callable[[], None]
) -> None:
    account = await register(client)
    key = uuid.uuid4().hex

    crash_before_revision()
    crashed = await post(client, account, key)
    retried = await post(client, account, key)  # the client never got an answer, so it retries

    assert (crashed.status_code, retried.status_code) == (500, 201)
    assert retried.headers["Idempotent-Replayed"] == "true"
    assert (await listed(client, account))["total"] == 1
    assert [item["action"] for item in await history(client, account, retried.json()["id"])] == [
        "create"
    ]


async def test_the_same_key_never_adds_twice(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    asha, ravi = await register(client), await register(other_client)
    key = uuid.uuid4().hex

    first = await post(client, asha, key)
    replay = await post(client, asha, key, title="A different body, same key")
    burst = await asyncio.gather(*(post(client, asha, key) for _ in range(5)))
    other_key = await post(client, asha, uuid.uuid4().hex)
    other_user = await post(other_client, ravi, key)  # keys are per user
    bad_key = await client.post(ENTRIES, headers=asha.headers | {KEY_HEADER: "short"}, json=entry())

    same = {first.json()["id"], replay.json()["id"], *(r.json()["id"] for r in burst)}
    assert len(same) == 1
    assert replay.json()["title"] == "Rice"  # the entry the first request made
    assert "Idempotent-Replayed" not in first.headers
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert (other_key.status_code, other_user.status_code, bad_key.status_code) == (201, 201, 422)
    assert other_user.json()["id"] not in same
    assert (await listed(client, asha))["total"] == 2
    assert len(await history(client, asha, first.json()["id"])) == 1


async def test_history_pages_and_outlives_deleting(client: AsyncClient) -> None:
    account = await register(client)
    first = await add(client, account)
    current = first
    for number in range(3):
        current = (await save(client, account, current, title=f"Edit {number}")).json()
    await client.delete(f"{ENTRIES}/{first['id']}", headers=account.headers)

    page = await client.get(
        f"{ENTRIES}/{first['id']}/revisions?page_size=2&page=2", headers=account.headers
    )

    assert [item["revision_no"] for item in page.json()["items"]] == [3, 2]
    assert (page.json()["total"], page.json()["has_next"]) == (5, True)


async def test_changes_show_in_the_activity_feed_once_each(client: AsyncClient) -> None:
    account = await register(client)
    key = uuid.uuid4().hex
    first = (await post(client, account, key)).json()
    await post(client, account, key)  # replayed: nothing new happened
    edited = (await save(client, account, first, title="Basmati rice")).json()
    await save(client, account, edited)  # saved unchanged: nothing happened
    await client.delete(f"{ENTRIES}/{first['id']}", headers=account.headers)
    await client.post(f"{ENTRIES}/{first['id']}/undelete", headers=account.headers)

    feed = (await client.get("/v1/activity", headers=account.headers)).json()["items"]

    assert [(item["type"], item["params"]) for item in reversed(feed)] == [
        ("expenses.entry_added", {"module": "expenses", "title": "Rice"}),
        ("expenses.entry_updated", {"module": "expenses", "title": "Basmati rice"}),
        ("expenses.entry_deleted", {"module": "expenses", "title": "Basmati rice"}),
        ("expenses.entry_undeleted", {"module": "expenses", "title": "Basmati rice"}),
    ]
