from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from tests.helpers import running_client

probe = APIRouter(prefix="/probe")


class ProbePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12)
    count: int


@probe.get("/boom")
async def boom() -> None:
    raise RuntimeError("kaboom: internal detail that must not leak")


@probe.get("/missing")
async def missing() -> None:
    raise NotFoundError(details={"id": "42"})


@probe.post("/items")
async def create_item(payload: ProbePayload) -> dict[str, int]:
    return {"count": payload.count}


@pytest.fixture
async def probe_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    app.include_router(probe)
    async with running_client(app) as http:
        yield http


def assert_envelope(body: dict[str, object], *, code: str, request_id: str) -> None:
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == code
    assert body["request_id"] == request_id


async def test_unknown_route_returns_not_found_envelope(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/does-not-exist")

    assert response.status_code == 404
    assert_envelope(response.json(), code="NOT_FOUND", request_id=response.headers["X-Request-ID"])


async def test_wrong_method_returns_method_not_allowed(probe_client: AsyncClient) -> None:
    response = await probe_client.delete("/healthz")

    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"


async def test_app_error_maps_to_its_status_code_and_details(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/probe/missing")

    assert response.status_code == 404
    body = response.json()
    assert_envelope(body, code="NOT_FOUND", request_id=response.headers["X-Request-ID"])
    assert body["message"] == "Resource not found."
    assert body["details"] == {"id": "42"}


async def test_validation_error_lists_fields_without_echoing_input(
    probe_client: AsyncClient,
) -> None:
    response = await probe_client.post(
        "/probe/items", json={"password": "hunter2", "count": "many", "extra": 1}
    )

    assert response.status_code == 422
    body = response.json()
    assert_envelope(body, code="VALIDATION_ERROR", request_id=response.headers["X-Request-ID"])
    locations = {tuple(error["loc"]) for error in body["details"]}
    assert locations == {("body", "password"), ("body", "count"), ("body", "extra")}
    assert all(set(error) == {"loc", "msg", "type"} for error in body["details"])
    assert "hunter2" not in response.text


async def test_unhandled_exception_returns_generic_500_with_request_id(
    probe_client: AsyncClient,
) -> None:
    response = await probe_client.get("/probe/boom", headers={"X-Request-ID": "trace-0001-abcd"})

    assert response.status_code == 500
    body = response.json()
    assert_envelope(body, code="INTERNAL_ERROR", request_id="trace-0001-abcd")
    assert response.headers["X-Request-ID"] == "trace-0001-abcd"
    assert body["message"] == "Internal server error."
    assert "kaboom" not in response.text
