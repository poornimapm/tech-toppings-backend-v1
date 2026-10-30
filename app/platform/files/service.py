"""``Files(scope, clock)``: how a module keeps a small private file for the scope's owner and
hands it back only to them: ``await Files(scope, clock).keep(request, module="labor",
purpose="worker_photo", types=..., max_bytes=...)``.

A file's type comes from its bytes (``sniff``), never from the client, and its size is checked
before it is kept. It is served only through the module's own routes, after their checks, with
headers that keep it out of shared caches and stop a browser guessing another type.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from typing import Any, ClassVar

from fastapi import Request, Response, status

from app.core.clock import Clock
from app.core.documents import OwnedDocument
from app.core.errors import AppError, NotFoundError
from app.core.repository import Scope, ScopedRepository, parse_object_id
from app.platform.agents.photos import sniff
from app.platform.files.models import StoredFile


class FileTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code: ClassVar[str] = "FILE_TOO_LARGE"
    default_message = "That file is too large."


class FileTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code: ClassVar[str] = "FILE_TYPE_UNSUPPORTED"
    default_message = "That kind of file can't be kept here."


class StoredFileRepository(ScopedRepository[StoredFile]):
    model: ClassVar[type[OwnedDocument]] = StoredFile

    def module_key_for(self, fields: Mapping[str, Any]) -> str:
        return str(fields["module_key"])


class Files:
    def __init__(self, scope: Scope, clock: Clock) -> None:
        self.repository = StoredFileRepository(scope, clock)

    async def keep(
        self,
        request: Request,
        *,
        module: str,
        purpose: str,
        types: Collection[str],
        max_bytes: int,
    ) -> StoredFile:
        """The request's body as a file of the owner's, checked first: at most ``max_bytes``,
        and one of ``types`` by its own bytes."""
        data = await request.body()
        if len(data) > max_bytes:
            raise FileTooLargeError(details={"max_bytes": max_bytes})
        kind = sniff(data)
        if kind is None or kind not in types:
            raise FileTypeError(details={"types": sorted(types)})
        return await self.repository.create(
            {
                "module_key": module,
                "purpose": purpose,
                "content_type": kind,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data": data,
            }
        )

    async def get(self, file_id: str, *, module: str) -> StoredFile:
        found = await self.repository.first({"_id": parse_object_id(file_id), "module_key": module})
        if found is None:
            raise NotFoundError
        return found

    async def forget(self, file_id: str | None) -> None:
        """Remove a file for good (replaced or taken away): its bytes don't linger."""
        object_id = parse_object_id(file_id) if file_id else None
        if object_id is not None:
            await StoredFile.get_pymongo_collection().delete_one(
                self.repository.query({"_id": object_id}, include_deleted=True)
            )


def file_response(stored: StoredFile) -> Response:
    """The file as a response its owner's browser may show, and nothing else keeps."""
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
