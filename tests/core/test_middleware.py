from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import ApiSettings, CorsSettings, SecuritySettings
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


PROXY_SECRET = "proxy-" * 8


@pytest.fixture
async def guarded(mongo_uri: str) -> AsyncIterator[AsyncClient]:
    """The API behind its /api proxy: it answers only what carries the proxy's secret."""
    security = SecuritySettings(_env_file=None, proxy_secret=SecretStr(PROXY_SECRET))
    async with running_client(create_app(build_settings(mongo_uri, security=security))) as http:
        yield http


async def test_through_the_proxy_the_api_answers(guarded: AsyncClient) -> None:
    response = await guarded.get("/readyz", headers={"X-TT-Proxy": PROXY_SECRET})

    assert response.status_code == 200


@pytest.mark.parametrize("given", [None, "", "wrong", PROXY_SECRET[:-1], PROXY_SECRET + "x"])
async def test_around_the_proxy_the_api_refuses(
    guarded: AsyncClient, capsys: pytest.CaptureFixture[str], given: str | None
) -> None:
    headers = {"X-Request-ID": "around-proxy-01"} | ({"X-TT-Proxy": given} if given else {})
    capsys.readouterr()

    response = await guarded.post("/v1/auth/login", headers=headers, json={})

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert response.json()["request_id"] == "around-proxy-01"
    assert response.headers["X-Content-Type-Options"] == "nosniff"  # still a hardened answer
    out = capsys.readouterr().out
    events = [e for e in json_log_lines(out) if e["event"] == "http_request"]
    assert [(e["path"], e["status"]) for e in events] == [("/v1/auth/login", 403)]
    assert PROXY_SECRET not in out


async def test_the_host_health_check_needs_no_proxy(guarded: AsyncClient) -> None:
    assert (await guarded.get("/healthz")).status_code == 200
    assert (await guarded.get("/readyz")).status_code == 403  # only the paths named


GZIP = {"Accept-Encoding": "gzip"}


async def test_a_big_answer_is_gzipped_for_whoever_accepts_it(client: AsyncClient) -> None:
    zipped = await client.get("/openapi.json", headers=GZIP)
    plain = await client.get("/openapi.json", headers={"Accept-Encoding": "identity"})

    assert zipped.headers["Content-Encoding"] == "gzip"
    assert zipped.json() == plain.json()  # the same answer, smaller on the wire
    assert "Content-Encoding" not in plain.headers
    assert "Content-Encoding" not in (await client.get("/healthz", headers=GZIP)).headers


async def test_sign_in_answers_are_never_gzipped(mongo_uri: str) -> None:
    api = ApiSettings(_env_file=None, gzip_min_bytes=1)
    async with running_client(create_app(build_settings(mongo_uri, api=api))) as http:
        signed_in = await http.post("/v1/auth/login", headers=GZIP, json={})
        elsewhere = await http.get("/healthz", headers=GZIP)

    assert "Content-Encoding" not in signed_in.headers
    assert elsewhere.headers["Content-Encoding"] == "gzip"


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
