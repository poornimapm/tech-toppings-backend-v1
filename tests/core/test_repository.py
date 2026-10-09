from __future__ import annotations

import pytest
from beanie import PydanticObjectId

from app.core.errors import NotFoundError
from app.core.pagination import PageParams
from app.core.repository import Scope, parse_object_id
from tests.fixtures.modules.notes.models import Note, NoteRepository
from tests.helpers import FrozenClock

ALICE = PydanticObjectId()
BOB = PydanticObjectId()
ADMIN = PydanticObjectId()
NEWEST_FIRST = [("created_at", -1)]


def test_scope_filters() -> None:
    assert Scope.for_user(ALICE).owner_filter() == {"user_id": ALICE}
    assert Scope.unrestricted(ADMIN).owner_filter() == {}
    assert Scope.unrestricted(ADMIN).actor_id == ADMIN


def test_malformed_ids_are_treated_as_missing() -> None:
    assert parse_object_id("nope") is None
    assert parse_object_id(str(ALICE)) == ALICE
    assert parse_object_id(ALICE) == ALICE


@pytest.mark.usefixtures("client")  # running app => Beanie initialised with Note
async def test_create_always_stamps_the_scope_owner(clock: FrozenClock) -> None:
    repo = NoteRepository(Scope.for_user(ALICE), clock)

    note = await repo.create({"text": "x", "user_id": BOB, "module_key": "spoofed"})

    assert note.user_id == ALICE
    assert note.module_key == "notes"
    assert note.created_by == ALICE
    assert note.created_at == clock.now()


@pytest.mark.usefixtures("client")
async def test_unrestricted_scope_cannot_create(clock: FrozenClock) -> None:
    with pytest.raises(ValueError, match="unrestricted"):
        await NoteRepository(Scope.unrestricted(ADMIN), clock).create({"text": "x"})


@pytest.mark.usefixtures("client")
async def test_soft_deleted_records_disappear_and_updates_are_audited(clock: FrozenClock) -> None:
    owner = PydanticObjectId()
    repo = NoteRepository(Scope.for_user(owner), clock)
    note = await repo.create({"text": "draft"})
    assert note.id is not None

    clock.advance(minutes=5)
    updated = await repo.update(note.id, {"text": "final"})
    await repo.soft_delete(note.id)
    items, total = await repo.list(PageParams(page=1, page_size=10), NEWEST_FIRST)

    assert updated.text == "final"
    assert updated.updated_at == clock.now()
    assert updated.updated_by == owner
    assert await repo.find(note.id) is None
    assert (items, total) == ([], 0)
    with pytest.raises(NotFoundError):
        await repo.update(note.id, {"text": "zombie"})
    with pytest.raises(NotFoundError):
        await repo.soft_delete(note.id)


@pytest.mark.usefixtures("client")
async def test_admin_scope_reads_across_owners(clock: FrozenClock) -> None:
    first, second = PydanticObjectId(), PydanticObjectId()
    a = await NoteRepository(Scope.for_user(first), clock).create({"text": "a"})
    b = await NoteRepository(Scope.for_user(second), clock).create({"text": "b"})

    admin = NoteRepository(Scope.unrestricted(ADMIN), clock)

    assert (await admin.get(a.id or "")).user_id == first
    assert (await admin.get(b.id or "")).user_id == second
    with pytest.raises(NotFoundError):
        await NoteRepository(Scope.for_user(first), clock).get(b.id or "")


@pytest.mark.usefixtures("client")
async def test_malformed_ids_raise_not_found(clock: FrozenClock) -> None:
    repo = NoteRepository(Scope.for_user(ALICE), clock)

    for call in (repo.get("bad"), repo.update("bad", {}), repo.soft_delete("bad")):
        with pytest.raises(NotFoundError):
            await call


@pytest.mark.usefixtures("client")
async def test_pagination_counts_everything_but_returns_one_page(clock: FrozenClock) -> None:
    repo = NoteRepository(Scope.for_user(PydanticObjectId()), clock)
    for index in range(5):
        clock.advance(seconds=1)
        await repo.create({"text": f"n{index}"})

    items, total = await repo.list(PageParams(page=2, page_size=2), NEWEST_FIRST)

    assert total == 5
    assert [n.text for n in items] == ["n2", "n1"]


def test_note_model_settings() -> None:
    assert Note.MODULE_KEY == "notes"
