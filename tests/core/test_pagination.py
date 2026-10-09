from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.pagination import InvalidSortError, Page, PageParams, parse_sort
from tests.helpers import register

ALLOWED = ("created_at", "text")


def test_parse_sort_supports_direction_and_multiple_fields() -> None:
    assert parse_sort("-created_at, text", ALLOWED, "created_at") == [
        ("created_at", -1),
        ("text", 1),
    ]
    assert parse_sort(None, ALLOWED, "-created_at") == [("created_at", -1)]
    assert parse_sort(" , ", ALLOWED, "text") == [("text", 1)]


def test_parse_sort_rejects_unlisted_fields() -> None:
    with pytest.raises(InvalidSortError) as raised:
        parse_sort("password_hash", ALLOWED, "created_at")

    assert raised.value.details == {"field": "password_hash", "allowed": ["created_at", "text"]}


def test_page_metadata() -> None:
    params = PageParams(page=2, page_size=3)

    page = Page[int].build([4, 5, 6], params, total=7)

    assert params.skip == 3
    assert (page.page, page.page_size, page.total, page.has_next) == (2, 3, 7, True)
    assert Page[int].build([7], PageParams(page=3, page_size=3), total=7).has_next is False


async def test_page_size_is_capped_by_configuration(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.get("/v1/test-notes?page_size=5000", headers=account.headers)
    default = await client.get("/v1/test-notes", headers=account.headers)
    invalid = await client.get("/v1/test-notes?page=0", headers=account.headers)

    assert response.json()["page_size"] == 100
    assert default.json()["page_size"] == 20
    assert invalid.status_code == 422
