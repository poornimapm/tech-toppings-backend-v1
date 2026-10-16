from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from app.core.concurrency import settle
from app.platform.context import ActorContext
from app.platform.plugins import owned_contributions
from app.platform.search.contract import SEARCH_ATTRIBUTE, SearchHit, SearchSpec


@dataclass(frozen=True)
class Searcher:
    owner: str  # the contributing package's key
    spec: SearchSpec


class SearchGroup(BaseModel):
    owner: str
    hits: list[SearchHit]


class SearchOut(BaseModel):
    query: str  # as searched for: trimmed, squeezed and cut to SEARCH_MAX_CHARS
    groups: list[SearchGroup]  # only modules that found something


def discover_search(packages: Iterable[str]) -> list[Searcher]:
    return [
        Searcher(owner, spec)
        for owner, spec in owned_contributions(packages, "search", SEARCH_ATTRIBUTE)
    ]


async def search_all(ctx: ActorContext, text: str, searchers: Sequence[Searcher]) -> SearchOut:
    """Every module at once; a slow or failing one is left out of the answer, not fatal."""
    settings = ctx.settings.search
    query = " ".join(text.split())[: settings.max_chars].strip()
    if len(query) < settings.min_chars:
        return SearchOut(query=query, groups=[])

    async def ask(item: Searcher) -> SearchGroup:
        hits = await settle(
            item.spec.fn(ctx, query, settings.max_hits),
            within=settings.timeout_seconds,
            event="search_failed",
            provider=item.spec.name,
        )
        return SearchGroup(owner=item.owner, hits=(hits or [])[: settings.max_hits])

    groups = await asyncio.gather(*(ask(item) for item in searchers))
    return SearchOut(query=query, groups=[group for group in groups if group.hits])
