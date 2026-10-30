"""Payments: money actually paid to a worker, apart from what their attendance earned. Any
number of them, partial or in full; one sent twice with the same key is kept once. A mistaken
payment is voided with a reason, never removed, so balances change in plain sight."""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

from pymongo.errors import DuplicateKeyError

from app.modules.labor.attendance import DayInFutureError, UnknownWorkerError
from app.modules.labor.audit import Audit, changes
from app.modules.labor.models import LaborPayment, PaymentRepository
from app.modules.labor.projects import Projects
from app.modules.labor.schemas import PaymentIn
from app.modules.labor.workers import Workers
from app.platform import ActorContext, ConflictError, NotFoundError, PageParams, parse_object_id

PAYMENT_FIELDS = ("worker_id", "day", "amount_minor", "method", "reference", "note")


class PaymentVoidedError(ConflictError):
    code: ClassVar[str] = "LABOR_PAYMENT_VOIDED"
    default_message = "That payment is voided already."


class Payments:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.projects = Projects(ctx)
        self.workers = Workers(ctx)
        self.repository = PaymentRepository(ctx.scope, ctx.clock)
        self.audit = Audit(ctx)

    async def add(
        self, project_id: str, body: PaymentIn, key: str | None
    ) -> tuple[LaborPayment, bool]:
        """(the payment, made now): a key already used answers its first payment."""
        if key is not None and (kept := await self.repository.first({"client_key": key})):
            return kept, False
        day = body.day or self.ctx.today()
        if day > self.ctx.today():
            raise DayInFutureError(details={"day": day.isoformat()})
        project = await self.projects.open_one(project_id)
        worker = await self.workers.get(body.worker_id)
        if worker.project_id != project.id:
            raise UnknownWorkerError(details={"worker_id": body.worker_id})
        fields: dict[str, Any] = {
            **body.model_dump(include={"amount_minor", "method", "reference", "note"}),
            "project_id": project.id,
            "worker_id": worker.id,
            "day": day.isoformat(),
            "client_key": key,
        }
        try:
            payment = await self.repository.create(fields)
        except DuplicateKeyError:  # the same key, sent twice at once: the first one stands
            kept = await self.repository.first({"client_key": key})
            if kept is None:
                raise
            return kept, False
        await self.audit.record(
            "payment",
            str(payment.id),
            "paid",
            project_id=project.id,
            changed=changes({}, {**fields, "worker_id": str(worker.id)}, PAYMENT_FIELDS),
            note=worker.code,
        )
        return payment, True

    async def page(
        self,
        project_id: str,
        params: PageParams,
        worker_id: str | None,
        start: date | None,
        end: date | None,
    ) -> tuple[list[LaborPayment], int]:
        project = await self.projects.get(project_id)
        criteria: dict[str, Any] = {"project_id": project.id}
        if worker_id:
            criteria["worker_id"] = parse_object_id(worker_id)
        days: dict[str, str] = {}
        if start:
            days["$gte"] = start.isoformat()
        if end:
            days["$lte"] = end.isoformat()
        if days:
            criteria["day"] = days
        return await self.repository.list(params, [("day", -1), ("_id", -1)], criteria)

    async def void(self, payment_id: str, reason: str) -> LaborPayment:
        payment = await self.repository.first({"_id": parse_object_id(payment_id)})
        if payment is None:
            raise NotFoundError
        project = await self.projects.open_one(str(payment.project_id))
        if payment.voided_at is not None:
            raise PaymentVoidedError
        voided = await self.repository.update(
            payment_id, {"voided_at": self.ctx.clock.now(), "void_reason": reason}
        )
        await self.audit.record(
            "payment",
            payment_id,
            "voided",
            project_id=project.id,
            changed=changes({"voided": False}, {"voided": True}, ("voided",)),
            note=reason,
        )
        return voided
