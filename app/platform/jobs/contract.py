"""The job contract.

A job is an async function ``(ctx: ActorContext) -> int`` in a package's ``jobs.py``, decorated
with ``@job("daily")`` or ``@job("each")``. It must:

* do what is due for ``ctx.user`` up to now;
* make every effect idempotent per item (a unique key, a guarded update): the bell's poll and
  the cron may run it at any time, even twice at once, so correctness never rests on the
  schedule or on a lock;
* return how many things it did.

``daily`` runs at most once per user and local day (a ``job_runs`` lease); ``each`` runs on
every catch-up, so it must be cheap when nothing is due.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from app.platform.context import ActorContext

JOB_ATTRIBUTE = "__platform_job__"
Cadence = Literal["daily", "each"]
JobFn = Callable[[ActorContext], Awaitable[int]]
F = TypeVar("F", bound=JobFn)


@dataclass(frozen=True)
class JobSpec:
    name: str
    cadence: Cadence
    fn: JobFn


def job(cadence: Cadence = "daily") -> Callable[[F], F]:
    def register(fn: F) -> F:
        target: Callable[..., Any] = fn
        if not inspect.iscoroutinefunction(target):
            raise TypeError(f"job {fn.__name__} must be async")
        setattr(fn, JOB_ATTRIBUTE, JobSpec(fn.__name__, cadence, fn))
        return fn

    return register
