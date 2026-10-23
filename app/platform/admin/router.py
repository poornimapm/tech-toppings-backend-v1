"""``/v1/admin``: the only place an unrestricted scope is allowed. Every route here needs the
admin role (``AdminUser``); a test asks each one as a user and as nobody."""

from __future__ import annotations

from fastapi import APIRouter

from app.platform.admin import allowlist, audit, dashboard, modules, usage, users

router = APIRouter(prefix="/admin", tags=["admin"])
for part in (dashboard, audit, users, allowlist, modules, usage):
    router.include_router(part.router)
