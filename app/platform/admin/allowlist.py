from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, ClassVar

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints
from pymongo.errors import DuplicateKeyError

from app.core.errors import ConflictError, NotFoundError, error_responses
from app.platform.admin.deps import AdminActionDep, AdminDep
from app.platform.auth.allowlist import AllowedEmail
from app.platform.users.repository import normalise_email

router = APIRouter(prefix="/allowed-emails")

Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class AllowedSource(StrEnum):
    SETTINGS = "settings"  # AUTH_ALLOWED_EMAILS: changed only in the deployment's settings
    ADMIN = "admin"  # added in the console


class AllowedOut(BaseModel):
    email: str
    source: AllowedSource
    note: str | None = None
    added_at: dt.datetime | None = None


class AllowedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    note: Note | None = None


class AllowedExistsError(ConflictError):
    code: ClassVar[str] = "ALLOWLIST_EXISTS"
    default_message = "That address may register already."


class AllowedInSettingsError(ConflictError):
    code: ClassVar[str] = "ALLOWLIST_IN_SETTINGS"
    default_message = "That address is in the deployment's settings; remove it there."


@router.get(
    "",
    summary="Who may register: from the settings and from the console",
    responses=error_responses(401, 403),
)
async def list_allowed(ctx: AdminDep) -> list[AllowedOut]:
    added = await AllowedEmail.find_all().sort("email").to_list()
    return [
        *(
            AllowedOut(email=email, source=AllowedSource.SETTINGS)
            for email in ctx.settings.auth.allowed_emails
        ),
        *(
            AllowedOut(
                email=item.email, source=AllowedSource.ADMIN, note=item.note, added_at=item.added_at
            )
            for item in added
        ),
    ]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Let an address register (audited)",
    responses=error_responses(401, 403, 409, 422),
)
async def add_allowed(body: AllowedIn, action: AdminActionDep) -> AllowedOut:
    email = normalise_email(str(body.email))
    if email in action.ctx.settings.auth.allowed_emails:
        raise AllowedExistsError
    item = AllowedEmail(
        email=email,
        note=body.note or None,
        added_by=action.ctx.user.id,
        added_at=action.ctx.clock.now(),
    )
    try:
        await item.insert()
    except DuplicateKeyError as exc:
        raise AllowedExistsError from exc
    await action.record("allowlist.added", entity="allowed_email", entity_id=email)
    return AllowedOut(
        email=email, source=AllowedSource.ADMIN, note=item.note, added_at=item.added_at
    )


@router.delete(
    "/{email}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop letting an address register (audited; accounts made already stay)",
    responses=error_responses(401, 403, 404, 409),
)
async def remove_allowed(
    email: Annotated[str, Path(max_length=320)], action: AdminActionDep
) -> None:
    email = normalise_email(email)
    if email in action.ctx.settings.auth.allowed_emails:
        raise AllowedInSettingsError
    removed = await AllowedEmail.get_pymongo_collection().delete_one({"email": email})
    if removed.deleted_count == 0:
        raise NotFoundError
    await action.record("allowlist.removed", entity="allowed_email", entity_id=email)
