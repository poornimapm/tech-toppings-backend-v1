from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.modules.expenses.features.entries.bulk import Bulk
from app.modules.expenses.features.entries.duplicates import Duplicates
from app.modules.expenses.features.entries.schemas import (
    BulkIn,
    BulkOut,
    DuplicatesIn,
    DuplicatesOut,
    EntryFilters,
    EntryIn,
    EntryListQuery,
    EntryOut,
    EntryUpdate,
    FacetsOut,
    RestoreIn,
    RevisionOut,
    SummaryOut,
)
from app.modules.expenses.features.entries.service import EntryService
from app.platform import (
    ActorDep,
    IdempotencyKey,
    Page,
    PageParamsDep,
    SettingsDep,
    answer_once,
    error_responses,
    page_params,
    parse_sort,
)

router = APIRouter(prefix="/entries")

SORT_FIELDS = ("date", "amount_minor", "created_at", "title")
DEFAULT_SORT = "-date,-created_at"


def entry_service(ctx: ActorDep) -> EntryService:
    return EntryService(ctx)


ServiceDep = Annotated[EntryService, Depends(entry_service)]


@router.get(
    "",
    summary=f"My entries (sort by {', '.join(SORT_FIELDS)})",
    responses=error_responses(401, 403),
)
async def list_entries(
    service: ServiceDep, settings: SettingsDep, query: Annotated[EntryListQuery, Query()]
) -> Page[EntryOut]:
    params = page_params(settings, query.page, query.page_size)
    entries, total = await service.entries.list(
        params,
        parse_sort(query.sort, SORT_FIELDS, DEFAULT_SORT),
        query.criteria(),
        include_deleted=query.deleted,
    )
    return Page[EntryOut].build([EntryOut.of(entry) for entry in entries], params, total)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add an entry (once per Idempotency-Key)",
    responses=error_responses(401, 403),
)
async def add_entry(
    body: EntryIn, service: ServiceDep, response: Response, key: IdempotencyKey = None
) -> EntryOut:
    return EntryOut.of(answer_once(response, await service.create(body, key)))


@router.get("/summary", summary="Totals of my entries", responses=error_responses(401, 403))
async def summarise_entries(
    service: ServiceDep, filters: Annotated[EntryFilters, Query()]
) -> SummaryOut:
    return await service.summary(filters)


@router.get(
    "/facets",
    summary="Tags, people and merchants I have used",
    responses=error_responses(401, 403),
)
async def entry_facets(service: ServiceDep) -> FacetsOut:
    return await service.facets()


@router.post(
    "/duplicates/check",
    summary="Saved entries that look like ones about to be saved (advice only)",
    responses=error_responses(401, 403),
)
async def check_duplicates(body: DuplicatesIn, ctx: ActorDep) -> DuplicatesOut:
    return await Duplicates(ctx).check(body)


@router.post(
    "/bulk-update",
    summary="One change to many entries: category, a tag, delete or bring back",
    responses=error_responses(401, 403, 422),
)
async def bulk_update(body: BulkIn, ctx: ActorDep) -> BulkOut:
    return await Bulk(ctx).apply(body)


@router.get(
    "/{entry_id}", summary="One entry (also when deleted)", responses=error_responses(401, 403, 404)
)
async def read_entry(entry_id: str, service: ServiceDep) -> EntryOut:
    return EntryOut.of(await service.entries.get(entry_id, include_deleted=True))


@router.put(
    "/{entry_id}",
    summary="Save an edited entry",
    responses=error_responses(401, 403, 404, 409),
)
async def save_entry(entry_id: str, body: EntryUpdate, service: ServiceDep) -> EntryOut:
    return EntryOut.of(await service.update(entry_id, body, body.revision_no))


@router.delete(
    "/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an entry (it can be brought back)",
    responses=error_responses(401, 403, 404),
)
async def delete_entry(entry_id: str, service: ServiceDep) -> None:
    await service.delete(entry_id)


@router.post(
    "/{entry_id}/undelete",
    summary="Bring a deleted entry back",
    responses=error_responses(401, 403, 404),
)
async def undelete_entry(entry_id: str, service: ServiceDep) -> EntryOut:
    return EntryOut.of(await service.undelete(entry_id))


@router.get(
    "/{entry_id}/revisions",
    summary="An entry's history, newest first",
    responses=error_responses(401, 403, 404),
)
async def entry_history(
    entry_id: str, service: ServiceDep, params: PageParamsDep
) -> Page[RevisionOut]:
    revisions, total = await service.entries.history(entry_id, params)
    return Page[RevisionOut].build([RevisionOut.of(item) for item in revisions], params, total)


@router.post(
    "/{entry_id}/restore",
    summary="Put an entry back to an earlier revision",
    responses=error_responses(401, 403, 404),
)
async def restore_entry(entry_id: str, body: RestoreIn, service: ServiceDep) -> EntryOut:
    return EntryOut.of(await service.restore(entry_id, body.revision_no))
