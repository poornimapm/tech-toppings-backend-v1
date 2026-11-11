"""Basic AI on a trip (Phase 19; its engine: ADR-0026). AI helps only on a trip in Basic mode
(``ensure_ai``), and it only ever drafts: the trip's pages preview a draft through Travel's own
routes, and the person applies it there. An apply that came from a run names it (``ai_run_id``),
checked here as the caller's own AI run that drafted it for this trip, so the history can say AI
made the change."""

from __future__ import annotations

from typing import ClassVar

from fastapi import status

from app.modules.travel.models import Trip, TripMode
from app.platform import (
    ActorContext,
    AgentMode,
    AgentRun,
    AppError,
    ConflictError,
    OwnedDocument,
    RunStatus,
    ScopedRepository,
)

# The client tools (tools.py) that hand a trip's pages a draft, by what applies it.
DAY_DRAFT = "travel_day_draft"
ITEMS_DRAFT = "travel_items_draft"
CHANGE_DRAFT = "travel_change_draft"
FINISHED = (RunStatus.COMPLETED, RunStatus.INCOMPLETE)  # incomplete: stopped after drafting


class AiOffError(ConflictError):
    code: ClassVar[str] = "TRAVEL_AI_OFF"
    default_message = "AI help is only for a trip in Basic AI. Switch it there, or go on by hand."


class AiRunInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_AI_RUN_INVALID"
    default_message = "That isn't an AI draft of yours for this trip."


class AiDraftAppliedError(ConflictError):
    code: ClassVar[str] = "TRAVEL_AI_DRAFT_APPLIED"
    default_message = "This AI draft was added already. Undo that first to add it again."


class RunRepository(ScopedRepository[AgentRun]):
    model: ClassVar[type[OwnedDocument]] = AgentRun


def ensure_ai(trip: Trip) -> Trip:
    """The trip, if AI may help on it: in Basic only (Premium stays locked until Phase 22)."""
    if trip.mode is not TripMode.BASIC:
        raise AiOffError(details={"mode": trip.mode})
    return trip


async def drafted_by(ctx: ActorContext, run_id: str | None, tool: str, trip: Trip) -> str | None:
    """The run an apply names, checked: the caller's own finished AI run (not one the static
    rules answered) that handed over this kind of draft for this trip. None: none was named."""
    if run_id is None:
        return None
    run = await RunRepository(ctx.scope, ctx.clock).find(run_id)
    drafted = (
        run is not None
        and run.mode is AgentMode.AGENT
        and run.status in FINISHED
        and any(
            action.tool == tool and action.arguments.get("trip_id") == str(trip.id)
            for action in run.client_actions
        )
    )
    if not drafted:
        raise AiRunInvalidError(details={"ai_run_id": run_id})
    return run_id
