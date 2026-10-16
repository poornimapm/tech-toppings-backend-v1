from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.modules.expenses.features.imports.reading import ImportTooLargeError
from app.modules.expenses.features.imports.rules import import_rules
from app.modules.expenses.features.imports.schemas import CommitIn, CommitOut, ImportIn, PreviewOut
from app.modules.expenses.features.imports.service import Imports
from app.platform import ActorDep, error_responses, rate_limit

router = APIRouter(prefix="/import")

ENVELOPE_BYTES = 64 * 1024  # the JSON around the file, and its mapping


async def within_size(request: Request) -> None:
    """A body too big for any file the import takes is refused before it is read: base64
    makes a file a third bigger."""
    most = import_rules().limits.max_bytes
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > most * 4 // 3 + ENVELOPE_BYTES:
        raise ImportTooLargeError(details={"max_bytes": most})


@router.post(
    "/preview",
    summary="Read a CSV file into entries, without saving: the columns, a guessed mapping, "
    "the first rows and the rows with problems",
    responses=error_responses(401, 403, 422, 429),
    dependencies=[Depends(rate_limit("imports")), Depends(within_size)],
)
async def preview_import(body: ImportIn, ctx: ActorDep) -> PreviewOut:
    return await Imports(ctx).preview(body)


@router.post(
    "/commit",
    summary="Add a batch of the file's importable rows (each once per file and row); "
    "send next_offset back for the rest",
    responses=error_responses(401, 403, 422, 429),
    dependencies=[Depends(rate_limit("imports")), Depends(within_size)],
)
async def commit_import(body: CommitIn, ctx: ActorDep) -> CommitOut:
    return await Imports(ctx).commit(body)
