from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from app.core.config import CorsSettings, SecuritySettings
from app.main import create_app
from tests.helpers import build_settings, json_log_lines, running_client


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


async def test_valid_incoming_request_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "client-trace_42.abc"})

    assert response.headers["X-Request-ID"] == "client-trace_42.abc"


@pytest.mark.parametrize("incoming", ["short", "has spaces in it", "x" * 129, "semi;colon!"])
async def test_invalid_incoming_request_id_is_replaced(client: AsyncClient, incoming: str) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": incoming})

    assert response.headers["X-Request-ID"] != incoming
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


async def test_security_headers_are_set_without_hsts_by_default(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in response.headers


async def test_hsts_header_when_configured(mongo_uri: str) -> None:
    settings = build_settings(
        mongo_uri, security=SecuritySettings(_env_file=None, hsts_max_age_seconds=31536000)
    )
    async with running_client(create_app(settings)) as http:
        response = await http.get("/healthz")

    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


async def test_cors_allows_only_configured_origins(mongo_uri: str) -> None:
    settings = build_settings(
        mongo_uri, cors=CorsSettings(_env_file=None, allowed_origins=("http://app.test",))
    )
    preflight = {"Access-Control-Request-Method": "GET"}
    async with running_client(create_app(settings)) as http:
        allowed = await http.options("/healthz", headers={"Origin": "http://app.test", **preflight})
        denied = await http.options("/healthz", headers={"Origin": "http://evil.test", **preflight})

    assert allowed.headers["Access-Control-Allow-Origin"] == "http://app.test"
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    assert "Access-Control-Allow-Origin" not in denied.headers


async def test_no_cors_headers_when_no_origins_configured(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"Origin": "http://app.test"})

    assert "Access-Control-Allow-Origin" not in response.headers


async def test_each_request_is_access_logged_with_request_id(
    capsys: pytest.CaptureFixture[str], client: AsyncClient
) -> None:
    capsys.readouterr()  # discard startup logs
    response = await client.get("/does-not-exist", headers={"X-Request-ID": "log-check-0001"})

    events = [e for e in json_log_lines(capsys.readouterr().out) if e["event"] == "http_request"]
    assert response.status_code == 404
    assert len(events) == 1
    event = events[0]
    assert event["request_id"] == "log-check-0001"
    assert event["method"] == "GET"
    assert event["path"] == "/does-not-exist"
    assert event["status"] == 404
    assert event["level"] == "info"
    assert isinstance(event["duration_ms"], float)


async def test_health_probes_are_logged_at_debug_level(
    capsys: pytest.CaptureFixture[str], client: AsyncClient
) -> None:
    capsys.readouterr()
    await client.get("/healthz")

    events = [e for e in json_log_lines(capsys.readouterr().out) if e["event"] == "http_request"]
    assert [e["level"] for e in events] == ["debug"]
