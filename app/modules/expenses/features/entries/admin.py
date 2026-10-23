"""``/v1/admin/expenses/entries``: everyone's entries for an admin, and an admin's correction.

A correction goes through the entry's own rules in its owner's data (their categories, budgets,
prices and activity feed); only who made it differs. Its revision names the admin as who
changed it, with the source ``admin``, and the audit trail keeps which fields changed and why,
never their values (plan: an admin edit writes a revision with ``changed_by=admin`` plus an
audit entry)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Annotated, Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.modules.expenses.domain import EntryKind, EntrySource, PaymentMode
from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.router import DEFAULT_SORT, SORT_FIELDS
from app.modules.expenses.features.entries.schemas import (
    Amount,
    CategoryId,
    EntryIn,
    EntryListQuery,
    EntryOut,
    Merchant,
    Note,
    Person,
    Tags,
    Title,
)
from app.modules.expenses.features.entries.service import EntryService
from app.platform import (
    AdminActionDep,
    AdminDep,
    NotFoundError,
    Page,
    Scope,
    SettingsDep,
    User,
    error_responses,
    page_params,
    parse_sort,
)

router = APIRouter(prefix="/entries")


class AdminEntryQuery(EntryListQuery):
    user_id: PydanticObjectId | None = Field(default=None, description="One person's only")


class AdminEntryOut(EntryOut):
    user_id: str

    @classmethod
    def of(cls, entry: Entry) -> AdminEntryOut:
        return cls.model_validate(
            {**EntryOut.of(entry).model_dump(), "user_id": str(entry.user_id)}
        )


class AdminEntryPatch(BaseModel):
    """A correction: only the fields sent change, the rest stay as they are."""

    model_config = ConfigDict(extra="forbid")

    revision_no: int = Field(ge=1, description="The revision corrected (409 if it moved on)")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]
    kind: EntryKind | None = None
    title: Title | None = None
    amount_minor: Amount | None = None
    category_id: CategoryId | None = None
    date: dt.date | None = None
    merchant: Merchant = None
    payment_mode: PaymentMode | None = None
    for_whom: Person = None
    tags: Tags | None = None
    note: Note = None


@router.get(
    "",
    summary="Everyone's entries, or one person's, with the list's filters",
    responses=error_responses(401, 403, 422),
)
async def list_all_entries(
    ctx: AdminDep, settings: SettingsDep, query: Annotated[AdminEntryQuery, Query()]
) -> Page[AdminEntryOut]:
    params = page_params(settings, query.page, query.page_size)
    criteria = query.criteria()
    if query.user_id is not None:
        criteria["user_id"] = query.user_id
    entries, total = await EntryRepository(ctx.scope, ctx.clock).list(
        params,
        parse_sort(query.sort, SORT_FIELDS, DEFAULT_SORT),
        criteria,
        include_deleted=query.deleted,
    )
    return Page[AdminEntryOut].build([AdminEntryOut.of(entry) for entry in entries], params, total)


@router.patch(
    "/{entry_id}",
    summary="Correct someone's entry: a revision by the admin, and an audit entry",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def correct_entry(
    entry_id: str, body: AdminEntryPatch, action: AdminActionDep
) -> AdminEntryOut:
    ctx = action.ctx
    current = await EntryRepository(ctx.scope, ctx.clock).get(entry_id)
    owner = await User.get(current.user_id)
    if owner is None:
        raise NotFoundError
    sent: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"revision_no", "reason"})
    try:
        edited = EntryIn.model_validate(
            {**current.model_dump(include=set(EntryIn.model_fields)), **sent}
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    # In the owner's data, as the admin: lookups stay theirs, the revision names the admin.
    as_owner = replace(
        ctx, user=owner, scope=Scope(actor_id=ctx.scope.actor_id, owner_id=current.user_id)
    )
    updated = await EntryService(as_owner).update(
        entry_id, edited, body.revision_no, source=EntrySource.ADMIN
    )
    changed = sorted(name for name in sent if getattr(current, name) != getattr(updated, name))
    if updated.revision_no != current.revision_no:
        await action.record(
            "entry.admin_corrected",
            entity="expense_entry",
            entity_id=entry_id,
            owner_id=current.user_id,
            details={"fields": changed, "revision_no": updated.revision_no, "reason": body.reason},
        )
    return AdminEntryOut.of(updated)
