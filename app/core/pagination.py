"""Pagination and sorting shared by every list endpoint (brief §7.4).

Clients send ``page``, ``page_size`` and ``sort=-created_at,name``; each endpoint declares
which fields may be sorted on, so arbitrary (unindexed or private) fields can't be used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, ClassVar, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel

from app.core.deps import SettingsDep
from app.core.errors import AppError, ErrorCode

T = TypeVar("T")


class InvalidSortError(AppError):
    status_code = 422
    code: ClassVar[str] = ErrorCode.VALIDATION_ERROR
    default_message = "Unsupported sort field."


@dataclass(frozen=True)
class PageParams:
    page: int
    page_size: int

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    settings: SettingsDep,
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    page_size: Annotated[int | None, Query(ge=1, description="Items per page")] = None,
) -> PageParams:
    size = page_size or settings.api.default_page_size
    return PageParams(page=page, page_size=min(size, settings.api.max_page_size))


PageParamsDep = Annotated[PageParams, Depends(page_params)]


def parse_sort(raw: str | None, allowed: Sequence[str], default: str) -> list[tuple[str, int]]:
    """``"-last_used_at,created_at"`` -> ``[("last_used_at", -1), ("created_at", 1)]``."""
    spec: list[tuple[str, int]] = []
    for part in (raw or default).split(","):
        token = part.strip()
        if not token:
            continue
        field, direction = (token[1:], -1) if token.startswith("-") else (token, 1)
        if field not in allowed:
            raise InvalidSortError(details={"field": field, "allowed": sorted(allowed)})
        spec.append((field, direction))
    return spec or parse_sort(default, allowed, default)


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    has_next: bool

    @classmethod
    def build(cls, items: list[T], params: PageParams, total: int) -> Page[T]:
        return cls(
            items=items,
            page=params.page,
            page_size=params.page_size,
            total=total,
            has_next=params.skip + len(items) < total,
        )
