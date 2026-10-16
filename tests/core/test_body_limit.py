"""The request body limit: any route refuses a body over ``API_MAX_BODY_BYTES`` (413, the
usual envelope) before reading it, whether it says its length or comes in chunks."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from app.core.config import ApiSettings, CorsSettings
from tests.helpers import build_app, build_settings, running_client

LIMIT = 2048
LOGIN = "/v1/auth/login"


@pytest.fixture
async def small_client(own_mongo_uri: str) -> AsyncIterator[AsyncClient]:
    settings = build_settings(
        own_mongo_uri,
        api=ApiSettings(_env_file=None, max_body_bytes=LIMIT),
        cors=CorsSettings(_env_file=None, allowed_origins=("https://app.example",)),
    )
    async with running_client(build_app(settings)) as client:
        yield client


def login(password_length: int) -> bytes:
    return json.dumps({"email": "a@example.com", "password": "p" * password_length}).encode()


async def chunks(body: bytes, size: int = 512) -> AsyncIterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


def refused(response_json: dict[str, object]) -> None:
    assert response_json["code"] == "REQUEST_TOO_LARGE"
    assert response_json["details"] == {"max_bytes": LIMIT}


async def test_a_body_that_says_it_is_too_large_is_refused_unread(
    small_client: AsyncClient,
) -> None:
    response = await small_client.post(
        LOGIN,
        content=login(LIMIT),
        headers={"Content-Type": "application/json", "Origin": "https://app.example"},
    )
    assert response.status_code == 413
    refused(response.json())
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    # Still a response of this API: readable cross-origin, with its security headers.
    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_body_sent_in_chunks_is_counted_as_it_comes(small_client: AsyncClient) -> None:
    response = await small_client.post(
        LOGIN, content=chunks(login(LIMIT)), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    refused(response.json())


async def test_a_chunked_body_within_the_limit_reaches_the_route_whole(
    small_client: AsyncClient,
) -> None:
    body = login(1000)
    assert len(body) > 512  # more than one chunk
    response = await small_client.post(
        LOGIN, content=chunks(body), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401  # read and checked: no such account


async def test_requests_within_the_limit_pass(small_client: AsyncClient) -> None:
    response = await small_client.post(
        LOGIN, content=login(20), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401
    assert (await small_client.get("/healthz")).status_code == 200
