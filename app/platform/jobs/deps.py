"""The lazy path (ADR-0007): a route that depends on ``CaughtUpDep`` first runs whatever is
due for the caller. The bell polls ``GET /v1/notifications``, so jobs keep up while the app
is open, with no scheduler."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.platform.context import ActorDep
from app.platform.jobs.runner import catch_up


async def caught_up(ctx: ActorDep) -> None:
    await catch_up(ctx, within=ctx.settings.jobs.lazy_timeout_seconds)


CaughtUpDep = Annotated[None, Depends(caught_up)]
