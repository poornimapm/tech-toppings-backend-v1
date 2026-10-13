"""``/v1/agents``: run skill-driven agents and confirm what they propose."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import ClockDep, SettingsDep
from app.core.errors import AppError, error_responses
from app.core.pagination import Page, PageParamsDep
from app.core.rate_limit import rate_limit
from app.platform.agents.runtime import AgentRunRepository, AgentRuntime, RunRequest
from app.platform.agents.schemas import AgentView, RunIn, RunOut, SkillView
from app.platform.agents.tools import Proposal, ToolContext
from app.platform.auth.deps import CurrentUser, UserScope

router = APIRouter(prefix="/agents", tags=["agents"])


class InputTooLongError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "AGENT_INPUT_TOO_LONG"
    default_message = "That request is too long."


def get_runtime(request: Request) -> AgentRuntime:
    return cast(AgentRuntime, request.app.state.agents)


def tool_context(
    request: Request, user: CurrentUser, scope: UserScope, clock: ClockDep, settings: SettingsDep
) -> ToolContext:
    return ToolContext(
        user=user, scope=scope, clock=clock, settings=settings, state=request.app.state
    )


RuntimeDep = Annotated[AgentRuntime, Depends(get_runtime)]
ContextDep = Annotated[ToolContext, Depends(tool_context)]


@router.get("", summary="Agents, and whether AI can run for me", responses=error_responses(401))
async def list_agents(
    runtime: RuntimeDep, ctx: ContextDep, params: PageParamsDep
) -> Page[AgentView]:
    catalog, user = runtime.catalog, ctx.user
    remaining = await runtime.quota.remaining(user.id) if user.id else 0
    views = []
    for agent in catalog.agents.values():
        availability = await runtime.availability(agent, user)
        skills = [
            SkillView(
                name=s.name,
                description=s.description,
                mode=runtime.settings.mode_for(s.name),
                has_static=s.static is not None,
            )
            for s in catalog.skills_of(agent)
        ]
        views.append(
            AgentView(
                name=agent.name,
                description=agent.description,
                mode=availability.mode,
                reason=availability.reason,
                fallback=runtime.settings.fallback,
                quota_remaining=remaining,
                skills=skills,
            )
        )
    page = views[params.skip : params.skip + params.page_size]
    return Page[AgentView].build(page, params, len(views))


@router.post(
    "/{name}/runs",
    status_code=status.HTTP_201_CREATED,
    summary="Ask an agent",
    dependencies=[Depends(rate_limit("agent_run"))],
    responses=error_responses(401, 404, 422, 429),
)
async def run_agent(name: str, body: RunIn, runtime: RuntimeDep, ctx: ContextDep) -> RunOut:
    if len(body.input) > runtime.settings.max_input_chars:
        raise InputTooLongError(details={"max": runtime.settings.max_input_chars})
    request = RunRequest(
        agent=name,
        input=body.input,
        skill=body.skill,
        locale=body.locale or ctx.user.locale,
        context=body.context.model_dump(),
    )
    return RunOut.of(await runtime.run(request, ctx))


@router.get("/runs/{run_id}", summary="One of my runs", responses=error_responses(401, 404))
async def read_run(run_id: str, runtime: RuntimeDep, scope: UserScope) -> RunOut:
    return RunOut.of(await AgentRunRepository(scope, runtime.clock).get(run_id))


@router.post(
    "/proposals/{proposal_id}/confirm",
    summary="Confirm a proposed change (runs it)",
    responses=error_responses(401, 404, 409),
)
async def confirm_proposal(proposal_id: str, runtime: RuntimeDep, ctx: ContextDep) -> Proposal:
    return await runtime.resolve(proposal_id, confirm=True, ctx=ctx)


@router.post(
    "/proposals/{proposal_id}/reject",
    summary="Reject a proposed change",
    responses=error_responses(401, 404, 409),
)
async def reject_proposal(proposal_id: str, runtime: RuntimeDep, ctx: ContextDep) -> Proposal:
    return await runtime.resolve(proposal_id, confirm=False, ctx=ctx)
