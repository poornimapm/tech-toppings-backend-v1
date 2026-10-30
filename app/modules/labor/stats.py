"""The figure on the module's Welcome tile: how many projects are open."""

from __future__ import annotations

from app.modules.labor.models import LaborProject, ProjectRepository, ProjectStatus
from app.platform import Clock, Scope, TileStat


async def open_projects(scope: Scope, clock: Clock) -> TileStat:
    criteria = ProjectRepository(scope, clock).query({"status": ProjectStatus.OPEN})
    return TileStat(value=await LaborProject.find(criteria).count(), label="items")
