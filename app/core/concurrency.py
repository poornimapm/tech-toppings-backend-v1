"""Run plugin code without letting it take the page down."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


async def settle(work: Awaitable[T], *, within: float, event: str, **context: object) -> T | None:
    """The result of ``work``, or None if it fails or takes longer than ``within`` seconds.

    Used for module-provided code (tile stats, dashboard widgets, jobs) and for each account
    in the jobs sweep: one broken or slow part is logged and skipped, never an error for the
    whole response."""
    try:
        return await asyncio.wait_for(work, timeout=within)
    except Exception as exc:
        logger.warning(event, error_type=type(exc).__name__, **context)
        return None
