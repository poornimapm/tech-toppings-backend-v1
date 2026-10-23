"""Exporting and erasing everything a person has here.

Nothing here knows any module: a record is someone's when its document has a ``user_id``
(every ``OwnedDocument`` and every revision), so a new module's data is included the day it
ships. Documents name the fields never handed out (``PRIVATE_FIELDS``: password hashes, token
digests, the TOTP secret). The audit trail is exported (without who else acted, from where) but
kept on erasure: it holds ids, not content, and expires after AUDIT_RETENTION_DAYS. Short-lived
counters that name the user (AI quota, job leases) expire on their own within days."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, ClassVar, cast

from beanie import Document, PydanticObjectId
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python

from app.core.errors import AppError, ConflictError
from app.platform.audit.models import AuditEntry
from app.platform.audit.service import AuditLog
from app.platform.auth.allowlist import AllowedEmail
from app.platform.auth.reauth import Reauth
from app.platform.auth.two_factor import TwoFactor
from app.platform.context import ActorContext
from app.platform.users.models import Role, User
from app.platform.users.repository import UserRepository, normalise_email

EXPORT_FORMAT = 1
BATCH = 500
# What an audit entry says about someone else who acted on this account (an admin): left out.
OTHERS_FIELDS = frozenset({"actor_id", "ip", "request_id"})

Clean = Callable[[Mapping[str, Any]], Any]


class ConfirmEmailError(AppError):
    status_code: ClassVar[int] = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "ACCOUNT_CONFIRM_MISMATCH"
    default_message = "Type your email address exactly to confirm."


class LastAdminError(ConflictError):
    code: ClassVar[str] = "ACCOUNT_LAST_ADMIN"
    default_message = "Make someone else an admin before deleting the only admin account."


class AccountDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_email: str = Field(min_length=3, max_length=320)
    password: str | None = Field(default=None, max_length=1024)  # if the account has one
    google_id_token: str | None = Field(default=None, max_length=8192)  # if it hasn't
    code: str | None = Field(default=None, max_length=32)  # if two-step sign-in is on


def _plain(raw: Mapping[str, Any], private: frozenset[str]) -> Any:
    return to_jsonable_python({k: v for k, v in raw.items() if k not in private}, fallback=str)


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _private(document: type[Document]) -> frozenset[str]:
    return cast(frozenset[str], getattr(document, "PRIVATE_FIELDS", frozenset()))


class AccountData:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        if ctx.user.id is None:  # pragma: no cover - persisted users always have an id
            raise AppError
        self.user_id: PydanticObjectId = ctx.user.id
        documents: list[type[Document]] = ctx.state.documents
        self.owned = [doc for doc in documents if "user_id" in doc.model_fields]
        self.audit = AuditLog(ctx.clock, ctx.settings)

    def filename(self) -> str:
        return f"my-data-{self.ctx.clock.now().date().isoformat()}.json"

    # ------------------------------------------------------------------ export

    async def export(self) -> AsyncIterator[bytes]:
        """The JSON file, written as it is read: never the whole of it in memory."""
        user = await User.get_pymongo_collection().find_one({"_id": self.user_id}) or {}
        head = {
            "format": EXPORT_FORMAT,
            "exported_at": to_jsonable_python(self.ctx.clock.now()),
            "account": _plain(user, User.PRIVATE_FIELDS),
        }
        yield _json(head)[:-1] + b',"collections":{'
        for index, (name, collection, owner, clean) in enumerate(self._sources()):
            yield (b"," if index else b"") + _json(name) + b":["
            cursor = collection.find({owner: self.user_id}).sort("_id", 1).batch_size(BATCH)
            buffer: list[bytes] = []
            written = 0
            async for raw in cursor:
                buffer.append(_json(clean(raw)))
                if len(buffer) == BATCH:
                    yield (b"," if written else b"") + b",".join(buffer)
                    written, buffer = written + len(buffer), []
            if buffer:
                yield (b"," if written else b"") + b",".join(buffer)
            yield b"]"
        yield b"}}"

    def _sources(self) -> list[tuple[str, Any, str, Clean]]:
        """(collection name, collection, owner field, how a record is cleaned) of each."""

        def owned_by(private: frozenset[str]) -> Clean:
            return lambda raw: _plain(raw, private)

        def audited(raw: Mapping[str, Any]) -> Any:
            mine = raw.get("actor_id") == self.user_id
            return _plain(raw, frozenset() if mine else OTHERS_FIELDS)

        sources: list[tuple[str, Any, str, Clean]] = [
            (
                doc.get_collection_name(),
                doc.get_pymongo_collection(),
                "user_id",
                owned_by(_private(doc)),
            )
            for doc in self.owned
        ]
        sources.append(
            (
                AuditEntry.get_collection_name(),
                AuditEntry.get_pymongo_collection(),
                "owner_id",
                audited,
            )
        )
        return sources

    async def record_export(self, ip: str | None) -> None:
        await self._record("account.exported", ip, {"collections": len(self.owned) + 1})

    # ------------------------------------------------------------------ erasure

    async def confirm(
        self, body: AccountDeleteIn, reauth: Reauth, two_factor: TwoFactor, ip: str | None
    ) -> None:
        """Proof it's really them, just now: the email typed out, the password (or a fresh
        Google sign-in), and a code when two-step sign-in is on."""
        user = self.ctx.user
        if normalise_email(body.confirm_email) != user.email:
            raise ConfirmEmailError
        await reauth.confirm(user, password=body.password, google_id_token=body.google_id_token)
        if user.two_factor:
            await two_factor.check(user, body.code or "", ip)
        if user.role is Role.ADMIN and await self._admins() <= 1:
            raise LastAdminError

    async def erase(self, ip: str | None) -> dict[str, int]:
        """Everything they own, then the account. Sweeping first means a failure halfway leaves
        a working account to delete again; then the account is signed out everywhere (nothing
        new can be written), swept again for what requests in flight wrote, and removed."""
        erased = await self._sweep()
        users = UserRepository(self.ctx.clock)
        await users.update(
            self.user_id, {"deleted_at": self.ctx.clock.now()}, increment={"token_version": 1}
        )
        if self.ctx.user.role is Role.ADMIN and await self._admins() == 0:
            # Another admin left at the same moment: one of the two must stay (its records are
            # already gone, but the console stays reachable).
            await users.update(self.user_id, {"deleted_at": None})
            raise LastAdminError
        for name, count in (await self._sweep()).items():
            erased[name] = erased.get(name, 0) + count
        # An invitation to register names them too; an admin can invite them again.
        await AllowedEmail.get_pymongo_collection().delete_one({"email": self.ctx.user.email})
        await User.get_pymongo_collection().delete_one({"_id": self.user_id})
        await self._record("account.deleted", ip, {"erased": erased})
        return erased

    async def _sweep(self) -> dict[str, int]:
        erased: dict[str, int] = {}
        for doc in self.owned:
            result = await doc.get_pymongo_collection().delete_many({"user_id": self.user_id})
            if result.deleted_count:
                erased[doc.get_collection_name()] = result.deleted_count
        return erased

    async def _admins(self) -> int:
        return await User.get_pymongo_collection().count_documents(
            {"role": Role.ADMIN, "deleted_at": None, "disabled_at": None}
        )

    async def _record(self, action: str, ip: str | None, details: dict[str, Any]) -> None:
        await self.audit.record(
            action,
            actor_id=self.user_id,
            entity="user",
            entity_id=str(self.user_id),
            owner_id=self.user_id,
            details=details,
            ip=ip,
        )
