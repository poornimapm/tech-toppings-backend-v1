"""The live smoke check (docs/deployment.md) passes a healthy site, names what a broken one
lacks, waits out a cold start, and never prints the test account's password."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from scripts.smoke_live import Smoke, main

APP, API = "https://app.example.test", "https://api.example.test"
EMAIL, PASSWORD = "smoke@example.com", "Smoke-Test-Password-1"
PAGE = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Type": "text/html",
}
GUARDED = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "X-Request-ID": "abc123def456",
}
PAGE_TEXT = '<script type="module" src="/assets/index-a1.js"></script>'
COOKIE = "tt_refresh=r; Path=/api/v1/auth; HttpOnly; Secure; SameSite=Strict"


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": "-"}, headers=GUARDED)


def healthy_site(mock: respx.MockRouter, **changes: httpx.Response) -> None:
    """A deployed site as the runbook leaves it; ``changes`` replace answers by route name."""
    answers: dict[str, httpx.Response] = {
        "page": httpx.Response(200, text=PAGE_TEXT, headers=PAGE),
        "deep": httpx.Response(200, text="<html>", headers=PAGE),
        "manifest": httpx.Response(200, json={"icons": [{"src": "/icons/icon-192.png"}]}),
        "worker": httpx.Response(200, headers={"Cache-Control": "no-cache"}),
        "asset": httpx.Response(200, headers={"Cache-Control": "public, immutable"}),
        "health": httpx.Response(200, json={"status": "ok"}),
        "ready": httpx.Response(200, json={"status": "ready"}),
        "me": error(401, "UNAUTHENTICATED"),
        "docs": httpx.Response(404),
        "openapi": httpx.Response(404),
        "direct": error(403, "FORBIDDEN"),
        "direct-health": httpx.Response(200),
        "login": httpx.Response(200, json={"access_token": "t"}, headers={"Set-Cookie": COOKIE}),
        "logout": httpx.Response(204),
    } | changes
    get = {
        "page": f"{APP}/",
        "deep": f"{APP}/m/expenses",
        "manifest": f"{APP}/manifest.webmanifest",
        "worker": f"{APP}/sw.js",
        "asset": f"{APP}/assets/index-a1.js",
        "health": f"{APP}/api/healthz",
        "ready": f"{APP}/api/readyz",
        "docs": f"{APP}/api/docs",
        "openapi": f"{APP}/api/openapi.json",
        "direct": f"{API}/v1/me",
        "direct-health": f"{API}/healthz",
    }
    for name, url in get.items():
        mock.get(url, name=name).mock(return_value=answers[name])

    def me(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer t":
            return httpx.Response(200, json={"email": EMAIL})
        return answers["me"]

    def refresh(request: httpx.Request) -> httpx.Response:
        if "X-TT-CSRF" not in request.headers:
            return error(403, "AUTH_CSRF_FAILED")
        return error(401, "AUTH_REFRESH_INVALID")

    mock.get(f"{APP}/api/v1/me").mock(side_effect=me)
    mock.post(f"{APP}/api/v1/auth/refresh").mock(side_effect=refresh)
    mock.post(f"{APP}/api/v1/auth/login", name="login").mock(return_value=answers["login"])
    mock.post(f"{APP}/api/v1/auth/logout").mock(return_value=answers["logout"])


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def run(**options: Any) -> dict[str, tuple[bool, str]]:
    with httpx.Client() as http:
        checks = Smoke(http, APP, API, sleep=lambda _: None, **options).run((EMAIL, PASSWORD))
    return {check.name: (check.ok, check.detail) for check in checks}


def test_a_healthy_site_passes_every_check(mock: respx.MockRouter) -> None:
    healthy_site(mock)

    results = run()

    assert [name for name, (ok, _) in results.items() if not ok] == []
    assert len(results) == 11


@pytest.mark.parametrize(
    ("change", "check", "detail"),
    [
        (
            {"page": httpx.Response(200, text=PAGE_TEXT, headers={"Content-Type": "text/html"})},
            "app page",
            "lacks",
        ),
        ({"worker": httpx.Response(200)}, "installable", "sw.js 200"),
        ({"asset": httpx.Response(200)}, "assets cached for good", ""),
        ({"ready": httpx.Response(503)}, "database reachable", "503"),
        ({"docs": httpx.Response(200)}, "api docs hidden", "/api/docs"),
        ({"direct": error(401, "UNAUTHENTICATED")}, "api answers only through the app", "401"),
        (
            {
                "login": httpx.Response(
                    200, json={"access_token": "t"}, headers={"Set-Cookie": "a=b"}
                )
            },
            "sign in and out",
            "cookie lacks httponly",
        ),
        (
            {"login": httpx.Response(200, json={"challenge_token": "c"})},
            "sign in and out",
            "two-step sign-in is on",
        ),
    ],
)
def test_a_broken_site_is_named_check_by_check(
    mock: respx.MockRouter, change: dict[str, httpx.Response], check: str, detail: str
) -> None:
    healthy_site(mock, **change)

    results = run()

    assert results[check][0] is False
    assert detail in results[check][1]
    assert sum(not ok for ok, _ in results.values()) == 1  # the others still pass


def test_a_sleeping_server_is_waited_for(mock: respx.MockRouter) -> None:
    healthy_site(mock)
    starting: list[httpx.Response | Exception] = [httpx.ConnectError("asleep"), httpx.Response(503)]
    mock.routes["health"].side_effect = [*starting, httpx.Response(200, json={"status": "ok"})]
    times = iter([0.0, 5.0, 10.0, 15.0])

    results = run(clock=lambda: next(times))

    assert results["api awake"] == (True, "after 15 s")


@pytest.mark.parametrize(
    ("health", "last"),
    [
        # The Pages Function without its settings: points at Pages, not at Render.
        (httpx.Response(500, json={"code": "INTERNAL_ERROR"}), "last: 500 INTERNAL_ERROR"),
        # The app's own page where the API should answer: /api never reached the API.
        (httpx.Response(200, text="<html>", headers=PAGE), "last: 200"),
    ],
)
def test_a_health_answer_that_isnt_the_apis_says_what_came_instead(
    mock: respx.MockRouter, health: httpx.Response, last: str
) -> None:
    healthy_site(mock, health=health, ready=httpx.Response(200, text="<html>", headers=PAGE))
    times = iter([0.0, 121.0])

    results = run(clock=lambda: next(times), wait=120)

    assert results["api awake"] == (False, f"not in 120 s ({last})")
    assert results["database reachable"][0] is False


def test_an_answer_it_can_not_read_fails_that_check_alone(mock: respx.MockRouter) -> None:
    healthy_site(mock, manifest=httpx.Response(200, text="<html>", headers=PAGE))
    mock.post(f"{APP}/api/v1/auth/login").mock(return_value=httpx.Response(200, text="<html>"))

    results = run()

    failed = {name for name, (ok, _) in results.items() if not ok}
    assert failed == {"installable", "sign in and out"}


def test_a_server_that_never_wakes_fails_once_the_wait_is_over(mock: respx.MockRouter) -> None:
    healthy_site(mock)
    mock.routes["health"].side_effect = httpx.ConnectError("down")
    times = iter([0.0, 60.0, 121.0])

    results = run(clock=lambda: next(times), wait=120)

    assert results["api awake"][0] is False


def test_the_password_and_tokens_are_never_printed(
    mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy_site(mock)
    monkeypatch.setenv("SMOKE_EMAIL", EMAIL)
    monkeypatch.setenv("SMOKE_PASSWORD", PASSWORD)

    assert main(["--app", APP, "--api", API, "--wait", "0"]) == 0

    out = capsys.readouterr().out
    assert "PASS  sign in and out" in out
    assert PASSWORD not in out
    assert "Bearer" not in out
