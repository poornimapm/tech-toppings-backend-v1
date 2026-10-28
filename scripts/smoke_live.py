"""Check the live app end to end, from outside, as a browser would (docs/deployment.md).

Usage: ``poe smoke-live --app https://<project>.pages.dev [--api https://<service>.onrender.com]``
With SMOKE_EMAIL and SMOKE_PASSWORD set (a test account without two-step sign-in, never an
admin's), it also signs in, reads the account and signs out; otherwise it changes nothing. One
line per check; exits 1 if any failed. It waits up to --wait seconds for a sleeping server.
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CSRF_HEADER = "X-TT-CSRF"
REFRESH_COOKIE_PATH = "/api/v1/auth"
RETRY_SECONDS = 5
# What every answer must carry: the site's own (public/_headers) and the API's.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "Strict-Transport-Security": "max-age=",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}
API_HEADERS = {
    "Strict-Transport-Security": "max-age=",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "X-Request-ID": "",
}


class SmokeAccount(BaseSettings):
    """The test account to sign in with; unset, sign-in isn't checked."""

    model_config = SettingsConfigDict(
        env_prefix="SMOKE_", extra="ignore", hide_input_in_errors=True
    )

    email: str | None = None
    password: SecretStr | None = None


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def missing(response: httpx.Response, expected: dict[str, str]) -> list[str]:
    """The expected headers an answer lacks, or holds without the expected part."""
    return [
        name
        for name, part in expected.items()
        if (value := response.headers.get(name)) is None or part not in value
    ]


def json_of(response: httpx.Response) -> dict[str, Any]:
    """The answer's JSON object, or {} for anything else (the app's page, say)."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def code_of(response: httpx.Response) -> str | None:
    code = json_of(response).get("code")
    return code if isinstance(code, str) else None


def says(response: httpx.Response, status: str) -> bool:
    """The API itself answered with this health status (not the app's page in its place)."""
    return response.status_code == HTTPStatus.OK and json_of(response).get("status") == status


def is_page(response: httpx.Response) -> bool:
    return response.status_code == HTTPStatus.OK and "text/html" in response.headers.get(
        "content-type", ""
    )


def said(response: httpx.Response, lacking: Sequence[str] = ()) -> str:
    return f"{response.status_code}" + (f", lacks {', '.join(lacking)}" if lacking else "")


class Smoke:
    def __init__(
        self,
        http: httpx.Client,
        app: str,
        api: str | None = None,
        *,
        wait: float = 120,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.http, self.app, self.api = http, app.rstrip("/"), api.rstrip("/") if api else None
        self.wait, self.sleep, self.clock = wait, sleep, clock

    def run(self, credentials: tuple[str, str] | None = None) -> list[Check]:
        checks: list[Callable[[], Check]] = [
            self.awake,  # first: the rest need a server that is up
            self.page,
            self.deep_link,
            self.installable,
            self.assets,
            self.ready,
            self.api_answers_safely,
            self.cookie_calls_need_csrf,
            self.docs_hidden,
        ]
        if self.api:
            checks.append(self.origin_locked)
        if credentials:
            checks.append(partial(self.sign_in, *credentials))
        return [self._safely(check) for check in checks]

    @staticmethod
    def _safely(check: Callable[[], Check]) -> Check:
        """A check that couldn't reach the site, or got an answer it can't read, fails alone;
        the others still run."""
        try:
            return check()
        except (httpx.HTTPError, ValueError) as exc:
            name = getattr(check, "func", check).__name__.replace("_", " ")
            return Check(name, False, type(exc).__name__)

    # ------------------------------------------------------------------ the site

    def awake(self) -> Check:
        """The API's own health answer, through /api, waiting out a cold start. What it last
        got is said: 500 'not connected' points at the Pages settings, a network error at
        this machine's connection."""
        started, last = self.clock(), ""
        while True:
            try:
                response = self.http.get(f"{self.app}/api/healthz")
                if says(response, "ok"):
                    return Check("api awake", True, f"after {self.clock() - started:.0f} s")
                last = said(response) + (f" {code_of(response)}" if code_of(response) else "")
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # still starting, or never reachable from here
            if self.clock() - started >= self.wait:
                return Check("api awake", False, f"not in {self.wait:.0f} s (last: {last})")
            self.sleep(RETRY_SECONDS)

    def page(self) -> Check:
        response = self.http.get(f"{self.app}/")
        lacking = missing(response, PAGE_HEADERS)
        return Check("app page", is_page(response) and not lacking, said(response, lacking))

    def deep_link(self) -> Check:
        response = self.http.get(f"{self.app}/m/expenses")
        return Check("deep link opens the app", is_page(response), said(response))

    def installable(self) -> Check:
        manifest = self.http.get(f"{self.app}/manifest.webmanifest")
        worker = self.http.get(f"{self.app}/sw.js")
        icons = manifest.status_code == HTTPStatus.OK and bool(json_of(manifest).get("icons"))
        fresh = "no-cache" in worker.headers.get("cache-control", "")
        ok = icons and worker.status_code == HTTPStatus.OK and fresh
        return Check("installable", ok, f"manifest {said(manifest)}, sw.js {said(worker)}")

    def assets(self) -> Check:
        found = re.search(r"/assets/[\w.-]+\.js", self.http.get(f"{self.app}/").text)
        if not found:
            return Check("assets cached for good", False, "no /assets/*.js in the page")
        response = self.http.get(f"{self.app}{found.group()}")
        cache = response.headers.get("cache-control", "")
        ok = response.status_code == HTTPStatus.OK and "immutable" in cache
        return Check("assets cached for good", ok, cache)

    # ------------------------------------------------------------------ the API, through /api

    def ready(self) -> Check:
        response = self.http.get(f"{self.app}/api/readyz")
        return Check("database reachable", says(response, "ready"), said(response))

    def api_answers_safely(self) -> Check:
        response = self.http.get(f"{self.app}/api/v1/me")
        lacking = missing(response, API_HEADERS)
        refused = (response.status_code, code_of(response)) == (
            HTTPStatus.UNAUTHORIZED,
            "UNAUTHENTICATED",
        )
        return Check(
            "api refuses strangers, hardened", refused and not lacking, said(response, lacking)
        )

    def cookie_calls_need_csrf(self) -> Check:
        url = f"{self.app}/api/v1/auth/refresh"
        without, with_header = self.http.post(url), self.http.post(url, headers={CSRF_HEADER: "1"})
        ok = (without.status_code, code_of(without), with_header.status_code) == (
            HTTPStatus.FORBIDDEN,
            "AUTH_CSRF_FAILED",
            HTTPStatus.UNAUTHORIZED,
        )
        return Check(
            "cookie calls need the csrf header", ok, f"{said(without)}, {said(with_header)}"
        )

    def docs_hidden(self) -> Check:
        found = [
            path
            for path in ("/api/docs", "/api/openapi.json")
            if self.http.get(f"{self.app}{path}").status_code != HTTPStatus.NOT_FOUND
        ]
        return Check("api docs hidden", not found, ", ".join(found))

    def origin_locked(self) -> Check:
        around = self.http.get(f"{self.api}/v1/me")
        health = self.http.get(f"{self.api}/healthz")
        ok = (around.status_code, code_of(around), health.status_code) == (
            HTTPStatus.FORBIDDEN,
            "FORBIDDEN",
            HTTPStatus.OK,
        )
        return Check("api answers only through the app", ok, f"{said(around)}, {said(health)}")

    def sign_in(self, email: str, password: str) -> Check:
        """Sign in, read the account, sign out. Never prints the password or a token."""
        name = "sign in and out"
        login = self.http.post(
            f"{self.app}/api/v1/auth/login",
            json={"email": email, "password": password},
            headers={CSRF_HEADER: "1"},
        )
        if login.status_code != HTTPStatus.OK:
            return Check(name, False, f"sign-in {said(login)} {code_of(login)}")
        token = json_of(login).get("access_token")
        if not token:
            return Check(name, False, "two-step sign-in is on: use an account without")
        cookie = ";".join(login.headers.get_list("set-cookie")).lower()
        flags = ["httponly", "secure", "samesite=strict", f"path={REFRESH_COOKIE_PATH}"]
        weak = [flag for flag in flags if flag not in cookie]
        me = self.http.get(f"{self.app}/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        out = self.http.post(f"{self.app}/api/v1/auth/logout", headers={CSRF_HEADER: "1"})
        read = me.status_code == HTTPStatus.OK and json_of(me).get("email") == email.lower()
        left = out.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT)
        if weak:
            return Check(name, False, f"cookie lacks {', '.join(weak)}")
        return Check(name, read and left, f"me {said(me)}, out {said(out)}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--app", required=True, help="The app's address, https://...pages.dev")
    parser.add_argument("--api", help="The API host's own address, to check it refuses strangers")
    parser.add_argument("--wait", type=float, default=120, help="Seconds to wait for a cold start")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    account = SmokeAccount()
    credentials = (
        (account.email, account.password.get_secret_value())
        if account.email and account.password
        else None
    )
    with httpx.Client(timeout=30, headers={"User-Agent": "tech-toppings-smoke"}) as http:
        checks = Smoke(http, args.app, args.api, wait=args.wait).run(credentials)
    for check in checks:
        detail = f": {check.detail}" if check.detail else ""
        print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}{detail}")
    if not credentials:
        print("(sign-in not checked: set SMOKE_EMAIL and SMOKE_PASSWORD to a test account)")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
