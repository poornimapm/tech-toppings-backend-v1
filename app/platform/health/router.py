"""``/healthz`` answers "is the process alive?" (no I/O, safe for frequent platform probes).
``/readyz`` answers "can it serve traffic?" (checks MongoDB; 503 + error envelope if not).

Both sit outside ``/v1`` because they describe the deployment, not the API contract.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import DatabaseDep, SettingsDep
from app.core.errors import ErrorResponse, ServiceNotReadyError
from app.platform.health.schemas import HealthStatus, ReadinessStatus

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz(settings: SettingsDep) -> HealthStatus:
    return HealthStatus(status="ok", name=settings.app.name, version=settings.app.version)


@router.get(
    "/readyz",
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
)
async def readyz(database: DatabaseDep) -> ReadinessStatus:
    if not await database.check_ready():
        raise ServiceNotReadyError(details={"checks": {"mongo": "unavailable"}})
    return ReadinessStatus(status="ready", checks={"mongo": "ok"})
