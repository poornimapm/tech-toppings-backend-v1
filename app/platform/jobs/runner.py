"""Running jobs: discovery, the daily lease, one user's catch-up, and the cron's sweep over
recently active users."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.core.clock import Clock
from app.core.concurrency import settle
from app.core.config import JobsSettings, Settings
from app.core.logging import get_logger
from app.core.repository import Scope
from app.platform.auth.models import AuthSession
from app.platform.context import ActorContext
from app.platform.jobs.contract import JOB_ATTRIBUTE, JobSpec
from app.platform.jobs.models import JobRun
from app.platform.modules.service import ModuleService
from app.platform.plugins import owned_contributions
from app.platform.users.repository import UserRepository

logger = get_logger(__name__)


@dataclass(frozen=True)
class Job:
    owner: str  # the contributing package's key: a module key, or a platform area
    spec: JobSpec


def discover_jobs(packages: Iterable[str]) -> tuple[Job, ...]:
    """Daily jobs come first, so what they schedule (a bill's reminder) is delivered by an
    ``each`` job in the same catch-up."""
    jobs = [
        Job(owner, spec) for owner, spec in owned_contributions(packages, "jobs", JOB_ATTRIBUTE)
    ]
    return tuple(sorted(jobs, key=lambda item: item.spec.cadence != "daily"))


class JobCounts(BaseModel):
    """Job runs that finished, were not due (done today, or held by another run) or failed,
    and how many things the finished ones did."""

    ran: int = 0
    skipped: int = 0
    failed: int = 0
    done: int = 0

    def __add__(self, other: JobCounts) -> JobCounts:
        return JobCounts(
            ran=self.ran + other.ran,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
            done=self.done + other.done,
        )


class JobsRunOut(JobCounts):
    users: int  # users caught up; disabled and deleted accounts are left out
    more: bool  # more active users than one run takes: the rest catch up on their next visit


class Leases:
    """A daily job runs once per user and local day. A claim is one guarded upsert on the
    unique (job_key, scope_key), as in ``Quota._take``: a duplicate key means the day is done
    or another run holds the lease. A run that fails keeps its lease until ``lock_until``,
    which is its backoff."""

    def __init__(self, clock: Clock, settings: JobsSettings) -> None:
        self.clock = clock
        self.settings = settings

    async def claim(self, job_key: str, scope_key: str, period: str) -> bool:
        key = {"job_key": job_key, "scope_key": scope_key}
        collection = JobRun.get_pymongo_collection()
        current = await collection.find_one(key, {"period": 1})
        if current is not None and current.get("period") == period:
            return False  # done today: the usual case costs one read and no write
        now = self.clock.now()
        free = {"$or": [{"lock_until": None}, {"lock_until": {"$lte": now}}]}
        lease = {"lock_until": now + timedelta(seconds=self.settings.lease_seconds)}
        try:
            await collection.update_one(
                {**key, "period": {"$ne": period}, **free},
                {"$set": lease, "$setOnInsert": {"purge_at": self._purge_at()}},
                upsert=True,
            )
        except DuplicateKeyError:
            return False
        return True

    async def finish(self, job_key: str, scope_key: str, period: str) -> None:
        await JobRun.get_pymongo_collection().update_one(
            {"job_key": job_key, "scope_key": scope_key},
            {
                "$set": {
                    "period": period,
                    "last_run_at": self.clock.now(),
                    "lock_until": None,
                    "purge_at": self._purge_at(),
                }
            },
        )

    def _purge_at(self) -> datetime:
        return self.clock.now() + timedelta(days=self.settings.retention_days)


async def _run(ctx: ActorContext, job: Job, leases: Leases) -> JobCounts:
    name, daily = job.spec.name, job.spec.cadence == "daily"
    scope_key, period = str(ctx.user.id), ctx.today().isoformat()
    if daily and not await leases.claim(name, scope_key, period):
        return JobCounts(skipped=1)
    done = await job.spec.fn(ctx)
    if daily:
        await leases.finish(name, scope_key, period)
    return JobCounts(ran=1, done=done)


async def catch_up(ctx: ActorContext, *, within: float, only: str | None = None) -> JobCounts:
    """Run what is due for the caller: the jobs of packages available to them (``only``: just
    that job). Each, its lease included, may take ``within`` seconds. Nothing here raises: a
    failing or slow job is logged and counted and the rest still run, so neither the bell's
    poll nor the cron's sweep fails because of one."""
    service = ModuleService(ctx.state.modules, ctx.scope, ctx.clock, ctx.settings)
    usable = await settle(service.usable_owners(), within=within, event="jobs_catch_up_failed")
    if usable is None:
        return JobCounts(failed=1)  # nothing could run this time; counted as one failure
    leases = Leases(ctx.clock, ctx.settings.jobs)
    counts = JobCounts()
    for item in ctx.state.jobs:
        name = item.spec.name
        if usable(item.owner) and only in (None, name):
            run = await settle(_run(ctx, item, leases), within=within, event="job_failed", job=name)
            counts += JobCounts(failed=1) if run is None else run
    return counts


async def _active_users(clock: Clock, settings: JobsSettings) -> list[PydanticObjectId]:
    """Users with a session used lately, most recent first; one more than a run takes, to tell
    whether there are more."""
    since = clock.now() - timedelta(days=settings.active_days)
    cursor = await AuthSession.get_pymongo_collection().aggregate(
        [
            {"$match": {"revoked_at": None, "last_used_at": {"$gte": since}}},
            {"$group": {"_id": "$user_id", "last_used_at": {"$max": "$last_used_at"}}},
            {"$sort": {"last_used_at": -1, "_id": 1}},
            {"$limit": settings.max_users_per_run + 1},
        ]
    )
    return [PydanticObjectId(row["_id"]) async for row in cursor]


async def run_cron(state: Any, clock: Clock, settings: Settings, only: str | None) -> JobsRunOut:
    """The cron's sweep: catch up each recently active user, as their own bell would. An
    account that can't be loaded is logged and left out, so it never stops the others."""
    limit, within = settings.jobs.max_users_per_run, settings.jobs.run_timeout_seconds
    user_ids = await _active_users(clock, settings.jobs)
    users, counts = 0, JobCounts()
    for user_id in user_ids[:limit]:
        loading = UserRepository(clock).by_id(user_id)
        user = await settle(loading, within=within, event="jobs_user_failed")
        if user is None or not user.is_active:
            continue
        ctx = ActorContext(user, Scope.for_user(user_id), clock, settings, state)
        counts += await catch_up(ctx, within=within, only=only)
        users += 1
    out = JobsRunOut(users=users, more=len(user_ids) > limit, **counts.model_dump())
    logger.info("jobs_cron_run", **out.model_dump())
    return out
