"""Test-only owned resource ("notes") exercising ScopedRepository through real HTTP routes.

It stands in for module data until Phase 5 (expenses) and is mounted only in tests: the
isolation matrix and the admin-vs-user tests run against it.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import ClockDep
from app.core.documents import OwnedDocument
from app.core.pagination import Page, PageParamsDep, parse_sort
from app.core.repository import Scope, ScopedRepository
from app.platform.auth.deps import AdminUser, UserScope


class Note(OwnedDocument):
    MODULE_KEY: ClassVar[str] = "test_notes"

    text: str

    class Settings:
        name = "test_notes"


class NoteRepository(ScopedRepository[Note]):
    model: ClassVar[type[OwnedDocument]] = Note


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200)


class NoteOut(BaseModel):
    id: str
    text: str
    user_id: str


def to_out(note: Note) -> NoteOut:
    return NoteOut(id=str(note.id), text=note.text, user_id=str(note.user_id))


SORT_FIELDS = ("created_at", "text")

router = APIRouter(prefix="/v1/test-notes", tags=["test-notes"])
admin_router = APIRouter(prefix="/v1/admin/test-notes", tags=["test-notes"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_note(body: NoteIn, scope: UserScope, clock: ClockDep) -> NoteOut:
    return to_out(await NoteRepository(scope, clock).create({"text": body.text}))


@router.get("")
async def list_notes(
    scope: UserScope,
    clock: ClockDep,
    params: PageParamsDep,
    sort: Annotated[str | None, Query()] = None,
) -> Page[NoteOut]:
    notes, total = await NoteRepository(scope, clock).list(
        params, parse_sort(sort, SORT_FIELDS, "-created_at")
    )
    return Page[NoteOut].build([to_out(n) for n in notes], params, total)


@router.get("/{note_id}")
async def get_note(note_id: str, scope: UserScope, clock: ClockDep) -> NoteOut:
    return to_out(await NoteRepository(scope, clock).get(note_id))


@router.patch("/{note_id}")
async def update_note(note_id: str, body: NoteIn, scope: UserScope, clock: ClockDep) -> NoteOut:
    return to_out(await NoteRepository(scope, clock).update(note_id, {"text": body.text}))


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: str, scope: UserScope, clock: ClockDep) -> None:
    await NoteRepository(scope, clock).soft_delete(note_id)


@admin_router.get("")
async def admin_list_notes(
    admin: AdminUser, clock: ClockDep, params: PageParamsDep
) -> Page[NoteOut]:
    assert admin.id is not None
    notes, total = await NoteRepository(Scope.unrestricted(admin.id), clock).list(
        params, parse_sort(None, SORT_FIELDS, "created_at")
    )
    return Page[NoteOut].build([to_out(n) for n in notes], params, total)
