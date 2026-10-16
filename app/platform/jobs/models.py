from __future__ import annotations

from datetime import datetime

from beanie import Document
from pymongo import ASCENDING, IndexModel


class JobRun(Document):
    """A daily job's lease for one user: the last local day it completed, and how long a run
    holds it. Removed ``JOBS_RETENTION_DAYS`` after the last run (TTL)."""

    job_key: str
    scope_key: str  # the user's id
    period: str | None = None  # last completed local day, YYYY-MM-DD
    lock_until: datetime | None = None  # a run in progress, or a failed run's backoff
    last_run_at: datetime | None = None
    purge_at: datetime

    class Settings:
        name = "job_runs"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("job_key", ASCENDING), ("scope_key", ASCENDING)],
                unique=True,
                name="job_scope_unique",
            ),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]
