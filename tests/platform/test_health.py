from __future__ import annotations

from httpx import AsyncClient

from app.main import create_app
from tests.helpers import UNREACHABLE_MONGO_URI, build_settings, mongo_settings, running_client


async def test_healthz_reports_process_alive(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "name": "Tech-Toppings API", "version": "0.1.0"}


async def test_readyz_is_ready_when_mongo_answers(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"mongo": "ok"}}


async def test_app_starts_and_readyz_returns_envelope_when_mongo_is_down() -> None:
    settings = build_settings(
        UNREACHABLE_MONGO_URI,
        mongo=mongo_settings(UNREACHABLE_MONGO_URI, server_selection_timeout_ms=200),
    )

    async with running_client(create_app(settings)) as http:
        live = await http.get("/healthz")
        ready = await http.get("/readyz")

    assert live.status_code == 200
    assert ready.status_code == 503
    body = ready.json()
    assert body["code"] == "SERVICE_NOT_READY"
    assert body["details"] == {"checks": {"mongo": "unavailable"}}
    assert body["request_id"] == ready.headers["X-Request-ID"]
