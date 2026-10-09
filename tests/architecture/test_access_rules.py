"""Guards from the reference analysis: auth is the default, and unrestricted data access is
only possible from admin routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from tests.helpers import api_operations

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# Every /v1 route not listed here must answer 401 to an anonymous caller.
PUBLIC_ROUTES = {
    ("GET", "/v1/auth/config"),
    ("POST", "/v1/auth/register"),
    ("POST", "/v1/auth/login"),
    ("POST", "/v1/auth/google"),
    # Cookie-authenticated (refresh token + CSRF header), not bearer:
    ("POST", "/v1/auth/refresh"),
    ("POST", "/v1/auth/logout"),
}
UNRESTRICTED_SCOPE_ALLOWED = ("platform/admin/",)  # plus its definition in core/repository.py


async def test_every_non_public_route_requires_a_bearer_token(
    app: FastAPI, client: AsyncClient
) -> None:
    open_routes = []
    operations = api_operations(app)
    assert len(operations) > len(PUBLIC_ROUTES)  # guard against an empty inventory
    for method, path in operations:
        if (method, path) in PUBLIC_ROUTES:
            continue
        url = re.sub(r"{\w+}", "000000000000000000000000", path)
        response = await client.request(method, url, json={})
        if response.status_code != 401:
            open_routes.append(f"{method} {path} -> {response.status_code}")

    assert open_routes == []


def test_public_routes_list_is_current(app: FastAPI) -> None:
    assert set(api_operations(app)) >= PUBLIC_ROUTES


def test_unrestricted_scope_is_only_used_by_admin_code() -> None:
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        relative = path.relative_to(APP_DIR).as_posix()
        if relative == "core/repository.py" or relative.startswith(UNRESTRICTED_SCOPE_ALLOWED):
            continue
        if "Scope.unrestricted(" in path.read_text(encoding="utf-8"):
            offenders.append(relative)

    assert offenders == []
