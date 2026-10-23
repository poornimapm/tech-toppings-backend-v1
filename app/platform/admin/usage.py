"""The admin's usage pages: who signs up and comes back, how the assistant answers (AI or its
rules), which errors are answered, and how full the database is. Counts only: nobody's
records."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.daterange import resolve_range
from app.core.errors import error_responses
from app.platform.admin.deps import AdminDep
from app.platform.admin.tally import ErrorTally
from app.platform.agents.models import AgentRun
from app.platform.auth.models import AuthSession
from app.platform.users.models import User

router = APIRouter(prefix="/analytics")
UTC = "UTC"  # usage counts UTC days, as the error tally does
MB = 1024 * 1024


class DayCount(BaseModel):
    date: dt.date
    count: int


class CodeCount(BaseModel):
    code: str
    count: int


class AiUsage(BaseModel):
    runs: int
    by_ai: int  # answered by a model
    by_rules: int  # answered by a skill's static rules (AI off, or as the fallback)
    fell_back: int  # rules answered because AI couldn't (no consent, quota, a provider down)
    fallback_rate_pct: float | None  # of the runs AI was asked for
    calls_by_provider: dict[str, int]


class UsageOut(BaseModel):
    start: dt.date
    end: dt.date
    total_users: int
    active_users: int  # signed in (or kept using a session) during the days
    signups: list[DayCount]
    ai: AiUsage
    errors: list[CodeCount]  # answered error codes, most first


class CollectionSize(BaseModel):
    name: str
    count: int
    size_mb: float
    storage_mb: float
    index_mb: float


class DbOut(BaseModel):
    database: str
    data_mb: float
    storage_mb: float
    index_mb: float
    quota_mb: int
    used_pct: float
    collections: list[CollectionSize]


async def _active(since: dt.datetime, until: dt.datetime) -> int:
    """Accounts signed in during the days: a device session that started before their end and
    was used after their start (sessions are kept AUTH_REFRESH_ABSOLUTE_TTL_SECONDS plus
    AUTH_SESSION_PURGE_GRACE_DAYS, so older days count fewer)."""
    users = await AuthSession.get_pymongo_collection().distinct(
        "user_id", {"created_at": {"$lt": until}, "last_used_at": {"$gte": since}}
    )
    return len(users)


async def _signups(since: dt.datetime, until: dt.datetime) -> list[DayCount]:
    cursor = await User.get_pymongo_collection().aggregate(
        [
            {"$match": {"created_at": {"$gte": since, "$lt": until}, "deleted_at": None}},
            {
                "$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id": 1}},
        ]
    )
    return [
        DayCount(date=dt.date.fromisoformat(row["_id"]), count=row["count"]) async for row in cursor
    ]


async def _ai(since: dt.datetime, until: dt.datetime) -> AiUsage:
    cursor = await AgentRun.get_pymongo_collection().aggregate(
        [
            {"$match": {"created_at": {"$gte": since, "$lt": until}}},
            {
                "$group": {
                    "_id": {
                        "mode": "$mode",
                        "fell_back": {"$ne": ["$reason", None]},
                        "provider": "$provider",
                    },
                    "runs": {"$sum": 1},
                    "calls": {"$sum": "$calls"},
                }
            },
        ]
    )
    rows = [row async for row in cursor]
    by_ai = sum(row["runs"] for row in rows if row["_id"]["mode"] == "agent")
    by_rules = sum(row["runs"] for row in rows if row["_id"]["mode"] == "static")
    fell_back = sum(
        row["runs"] for row in rows if row["_id"]["mode"] == "static" and row["_id"]["fell_back"]
    )
    providers: dict[str, int] = {}
    for row in rows:
        if row["_id"]["provider"]:
            providers[row["_id"]["provider"]] = (
                providers.get(row["_id"]["provider"], 0) + row["calls"]
            )
    asked = by_ai + fell_back
    return AiUsage(
        runs=sum(row["runs"] for row in rows),
        by_ai=by_ai,
        by_rules=by_rules,
        fell_back=fell_back,
        fallback_rate_pct=round(fell_back / asked * 100, 1) if asked else None,
        calls_by_provider=providers,
    )


async def _errors(since: dt.datetime, until: dt.datetime) -> list[CodeCount]:
    first, last = since.date().isoformat(), (until - dt.timedelta(days=1)).date().isoformat()
    cursor = await ErrorTally.get_pymongo_collection().aggregate(
        [
            {"$match": {"day": {"$gte": first, "$lte": last}}},
            {"$group": {"_id": "$code", "count": {"$sum": "$times"}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
    )
    return [CodeCount(code=row["_id"], count=row["count"]) async for row in cursor]


@router.get(
    "/usage",
    summary="Sign-ups, active users, assistant answers and errors, by day",
    responses=error_responses(401, 403, 422),
)
async def read_usage(
    ctx: AdminDep,
    start: Annotated[dt.date | None, Query(alias="from")] = None,
    end: Annotated[dt.date | None, Query(alias="to")] = None,
) -> UsageOut:
    admin = ctx.settings.admin
    span = resolve_range(
        start,
        end,
        today=ctx.clock.now().date(),
        timezone=UTC,
        default_days=admin.usage_default_days,
        max_days=admin.usage_max_days,
    )
    since, until = span.begins, span.ends
    total, active, signups, ai, errors = await asyncio.gather(
        User.get_pymongo_collection().count_documents({"deleted_at": None}),
        _active(since, until),
        _signups(since, until),
        _ai(since, until),
        _errors(since, until),
    )
    return UsageOut(
        start=since.date(),
        end=(until - dt.timedelta(days=1)).date(),
        total_users=total,
        active_users=active,
        signups=signups,
        ai=ai,
        errors=errors,
    )


def _mb(value: Any) -> float:
    return round(float(value or 0) / MB, 2)


@router.get(
    "/db",
    summary="How full the database is, and its collections' sizes",
    responses=error_responses(401, 403),
)
async def read_db(ctx: AdminDep) -> DbOut:
    database = User.get_pymongo_collection().database
    stats = await database.command("dbStats")
    collections = []
    for name in sorted(await database.list_collection_names()):
        cursor = await database[name].aggregate([{"$collStats": {"storageStats": {}}}])
        rows = [row async for row in cursor]
        storage: dict[str, Any] = rows[0].get("storageStats", {}) if rows else {}
        collections.append(
            CollectionSize(
                name=name,
                count=int(storage.get("count", 0)),
                size_mb=_mb(storage.get("size")),
                storage_mb=_mb(storage.get("storageSize")),
                index_mb=_mb(storage.get("totalIndexSize")),
            )
        )
    quota = ctx.settings.admin.db_quota_mb
    storage_mb = _mb(stats.get("storageSize")) + _mb(stats.get("indexSize"))
    return DbOut(
        database=database.name,
        data_mb=_mb(stats.get("dataSize")),
        storage_mb=_mb(stats.get("storageSize")),
        index_mb=_mb(stats.get("indexSize")),
        quota_mb=quota,
        used_pct=round(storage_mb / quota * 100, 1),
        collections=sorted(collections, key=lambda item: -item.storage_mb),
    )
