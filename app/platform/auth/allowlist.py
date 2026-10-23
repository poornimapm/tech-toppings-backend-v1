"""Who may register while registration is by allowlist (ADR-0011): the addresses in
AUTH_ALLOWED_EMAILS, and those an admin adds in the console."""

from __future__ import annotations

from datetime import datetime

from beanie import Document, PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.core.config import AuthSettings


class AllowedEmail(Document):
    email: str  # lower-cased
    note: str | None = None
    added_by: PydanticObjectId | None = None
    added_at: datetime

    class Settings:
        name = "allowed_emails"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("email", ASCENDING)], unique=True, name="email_unique")
        ]


async def is_allowed(email: str, settings: AuthSettings) -> bool:
    """``email`` (lower-cased) is on the settings' list or the admins'."""
    return email in settings.allowed_emails or (
        await AllowedEmail.find_one({"email": email}) is not None
    )
