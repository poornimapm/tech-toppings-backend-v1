"""``/v1/internal/jobs``: the jobs-cron workflow's way in (ADR-0007). It authenticates with a
shared token (``JOBS_TOKEN``), not a user's, and is left out of the OpenAPI schema because no
client calls it; an architecture test keeps it the only hidden route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.core.deps import ClockDep, SettingsDep
from app.core.errors import NotFoundError, UnauthenticatedError
from app.core.rate_limit import rate_limit
from app.core.security import same_secret
from app.platform.jobs.runner import JobsRunOut, run_cron

router = APIRouter(prefix="/internal/jobs", tags=["internal"], include_in_schema=False)

JOB_TOKEN_HEADER = "X-Job-Token"  # noqa: S105 - a header name, not a secret
ALL_JOBS = "all"


def require_job_token(
    settings: SettingsDep, token: Annotated[str | None, Header(alias=JOB_TOKEN_HEADER)] = None
) -> None:
    """An unset, missing or wrong token is a 401 before anything else is looked at."""
    if not same_secret(token, settings.jobs.token):
        raise UnauthenticatedError


@router.post(
    "/{job_key}/run",
    summary="Run due jobs for recently active users",
    dependencies=[Depends(rate_limit("jobs_run")), Depends(require_job_token)],
)
async def run_jobs(
    job_key: str, request: Request, clock: ClockDep, settings: SettingsDep
) -> JobsRunOut:
    """``job_key`` is a job's name, or ``all``."""
    state = request.app.state
    if job_key != ALL_JOBS and job_key not in {item.spec.name for item in state.jobs}:
        raise NotFoundError(details={"job": job_key})
    return await run_cron(state, clock, settings, None if job_key == ALL_JOBS else job_key)
