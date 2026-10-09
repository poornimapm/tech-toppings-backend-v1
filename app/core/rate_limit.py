"""Request rate limiting on top of the ``limits`` library.

One ``RateLimiter`` per app (``app.state.rate_limiter``), so tests and apps never share counters.
Routes opt in with ``Depends(rate_limit("auth_login"))``: the name refers to a field of
``RateLimitSettings`` (e.g. ``RATE_LIMIT_AUTH_LOGIN=10/minute``); keys are client IP + scope.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import Request
from limits import RateLimitItem, parse
from limits.aio.storage import Storage
from limits.aio.strategies import MovingWindowRateLimiter
from limits.storage import storage_from_string

from app.core.config import RateLimitSettings
from app.core.errors import RateLimitedError


class RateLimiter:
    def __init__(self, settings: RateLimitSettings) -> None:
        self.enabled = settings.enabled
        self._settings = settings
        storage = cast(Storage, storage_from_string(settings.storage_uri))
        self._strategy = MovingWindowRateLimiter(storage)

    def item(self, name: str) -> RateLimitItem:
        return parse(str(getattr(self._settings, name)))

    async def check(self, name: str, *keys: str) -> None:
        """Count one hit; raise ``RateLimitedError`` once the window is full."""
        if not self.enabled:
            return
        item = self.item(name)
        if await self._strategy.hit(item, name, *keys):
            return
        stats = await self._strategy.get_window_stats(item, name, *keys)
        retry_after = max(1, math.ceil(stats.reset_time - time.time()))
        raise RateLimitedError(retry_after)


def client_ip(request: Request) -> str:
    # uvicorn rewrites request.client from X-Forwarded-For for trusted proxies
    # (FORWARDED_ALLOW_IPS), so this is the real client behind Render/Cloudflare.
    return request.client.host if request.client else "unknown"


def rate_limit(name: str) -> Callable[[Request], Awaitable[None]]:
    """Dependency factory: ``dependencies=[Depends(rate_limit("auth_login"))]``."""

    async def dependency(request: Request) -> None:
        limiter = cast(RateLimiter, request.app.state.rate_limiter)
        await limiter.check(name, client_ip(request))

    return dependency
