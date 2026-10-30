"""What Labor Management keeps, all owned by one owner (``user_id``) and, below the project,
by one project: projects, their workers, each worker's wage history, attendance by day,
payments, and the audit trail of every change. Days are YYYY-MM-DD; money is in minor units
(paise) of the owner's currency, as everywhere in the app.

Nothing here is ever hard-deleted: a project is closed, a worker made inactive, a payment
voided, and the audit trail only grows."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.platform import OwnedDocument, ScopedRepository

MODULE = "labor"


class _Owned(OwnedDocument):
    MODULE_KEY: ClassVar[str] = MODULE


class Rates(BaseModel):
    """A project's default daily wage for a new worker: one for everyone, or one by gender."""

    by_gender: bool = False
    default_minor: int = 0
    female_minor: int = 0
    male_minor: int = 0


class ProjectStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class LaborProject(_Owned):
    name: str
    kind: str | None = None
    description: str | None = None
    icon: str | None = None  # an emoji
    started_on: str
    notes: str | None = None
    status: ProjectStatus = ProjectStatus.OPEN
    closed_at: datetime | None = None
    currency: str
    rates: Rates = Field(default_factory=Rates)
    next_code: int = 1  # the next worker number given by default

    class Settings:
        name = "labor_projects"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("user_id", ASCENDING), ("status", ASCENDING), ("started_on", DESCENDING)]),
        ]


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"


class LaborWorker(_Owned):
    project_id: PydanticObjectId
    name: str
    code: str
    gender: Gender | None = None
    phone: str | None = None
    daily_wage_minor: int
    active: bool = True
    notes: str | None = None
    photo_id: str | None = None  # a StoredFile
    id_card_id: str | None = None  # a StoredFile: a picture of their ID, if given

    class Settings:
        name = "labor_workers"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("user_id", ASCENDING), ("project_id", ASCENDING), ("code", ASCENDING)],
                unique=True,
                partialFilterExpression={"deleted_at": None},
                name="worker_code_unique",
            ),
            IndexModel([("user_id", ASCENDING), ("project_id", ASCENDING), ("name", ASCENDING)]),
        ]


class LaborWageChange(_Owned):
    """A worker's daily wage, from one rate to another, from a day on: past attendance keeps
    the rate it was recorded with."""

    project_id: PydanticObjectId
    worker_id: PydanticObjectId
    from_minor: int | None  # None: the first wage, given when the worker was registered
    to_minor: int
    effective_on: str
    reason: str | None = None

    class Settings:
        name = "labor_wage_changes"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("user_id", ASCENDING), ("worker_id", ASCENDING), ("created_at", DESCENDING)]
            ),
        ]


class AttendanceStatus(StrEnum):
    FULL = "full"
    HALF = "half"
    ABSENT = "absent"


class LaborAttendance(_Owned):
    """One worker's day in one project: never two (a unique index), and each keeps the rate it
    was recorded with, so a later wage change never rewrites it."""

    project_id: PydanticObjectId
    worker_id: PydanticObjectId
    day: str
    status: AttendanceStatus
    units: float  # 1, 0.5 or 0 (data/labor.yaml)
    rate_minor: int
    amount_minor: int
    note: str | None = None

    class Settings:
        name = "labor_attendance"
        indexes = [  # noqa: RUF012
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("project_id", ASCENDING),
                    ("worker_id", ASCENDING),
                    ("day", ASCENDING),
                ],
                unique=True,
                name="attendance_one_a_day",
            ),
            IndexModel([("user_id", ASCENDING), ("project_id", ASCENDING), ("day", ASCENDING)]),
        ]


class PaymentMethod(StrEnum):
    CASH = "cash"
    UPI = "upi"
    BANK = "bank"
    OTHER = "other"


class LaborPayment(_Owned):
    """Money paid to a worker. A mistaken one is voided (with a reason), never removed."""

    project_id: PydanticObjectId
    worker_id: PydanticObjectId
    day: str
    amount_minor: int
    method: PaymentMethod | None = None
    reference: str | None = None
    note: str | None = None
    voided_at: datetime | None = None
    void_reason: str | None = None
    client_key: str | None = None  # the Idempotency-Key it was sent with: kept once

    class Settings:
        name = "labor_payments"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("user_id", ASCENDING), ("client_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"client_key": {"$type": "string"}},
                name="payment_once_per_key",
            ),
            IndexModel([("user_id", ASCENDING), ("project_id", ASCENDING), ("day", DESCENDING)]),
            IndexModel([("user_id", ASCENDING), ("worker_id", ASCENDING), ("day", DESCENDING)]),
        ]


class Change(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class LaborAuditEntry(_Owned):
    """One change, as the server made it: who, when, to what, and each field from and to.
    Written only by the services; no route changes or removes one."""

    at: datetime
    actor_id: PydanticObjectId
    actor_name: str
    actor_role: str
    project_id: PydanticObjectId | None = None
    entity: str  # project, worker, rates, attendance, payment
    entity_id: str
    action: str  # created, updated, closed, reopened, deactivated, activated, ...
    changes: list[Change] = Field(default_factory=list)
    note: str | None = None
    request_id: str | None = None

    class Settings:
        name = "labor_audit"
        indexes = [  # noqa: RUF012
            IndexModel([("user_id", ASCENDING), ("at", DESCENDING)]),
            IndexModel([("user_id", ASCENDING), ("project_id", ASCENDING), ("at", DESCENDING)]),
            IndexModel([("entity", ASCENDING), ("entity_id", ASCENDING)]),
        ]


class ProjectRepository(ScopedRepository[LaborProject]):
    model: ClassVar[type[OwnedDocument]] = LaborProject


class WorkerRepository(ScopedRepository[LaborWorker]):
    model: ClassVar[type[OwnedDocument]] = LaborWorker


class WageChangeRepository(ScopedRepository[LaborWageChange]):
    model: ClassVar[type[OwnedDocument]] = LaborWageChange


class AttendanceRepository(ScopedRepository[LaborAttendance]):
    model: ClassVar[type[OwnedDocument]] = LaborAttendance


class PaymentRepository(ScopedRepository[LaborPayment]):
    model: ClassVar[type[OwnedDocument]] = LaborPayment


class AuditRepository(ScopedRepository[LaborAuditEntry]):
    model: ClassVar[type[OwnedDocument]] = LaborAuditEntry
