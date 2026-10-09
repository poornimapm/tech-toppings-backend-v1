"""Base Beanie documents.

``TimestampedDocument``: created/updated timestamps (set by repositories from the injected
Clock, never by the database or ``datetime.now()``).

``OwnedDocument``: every user-owned record carries ``user_id`` and ``module_key`` and is
soft-deleted via ``deleted_at`` (brief §7.3). Only ``ScopedRepository`` reads or writes them, so
the owner filter can never be forgotten.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from beanie import Document, PydanticObjectId
from pydantic import Field


class TimestampedDocument(Document):
    created_at: datetime
    updated_at: datetime


class OwnedDocument(TimestampedDocument):
    # Subclasses set this to their module key ("platform" for platform-owned records).
    MODULE_KEY: ClassVar[str]

    user_id: PydanticObjectId
    module_key: str
    created_by: PydanticObjectId
    updated_by: PydanticObjectId
    deleted_at: datetime | None = Field(default=None)
