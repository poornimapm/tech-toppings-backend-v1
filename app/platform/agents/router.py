"""``/v1/agents``: run skill-driven agents and confirm what they propose."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status

from app.core.config import Locale
from app.core.errors import AppError, error_responses
from app.core.pagination import Page, PageParamsDep
from app.core.rate_limit import rate_limit
from app.platform.agents.photos import SIGNATURES, Photo, sniff
from app.platform.agents.runtime import AgentRunRepository, AgentRuntime, RunRequest
from app.platform.agents.schemas import AgentView, RunIn, RunOut, SkillView
from app.platform.agents.tools import Proposal
from app.platform.auth.deps import UserScope
from app.platform.context import ActorDep
from app.platform.modules.service import ModuleService

router = APIRouter(prefix="/agents", tags=["agents"])


class InputTooLongError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "AGENT_INPUT_TOO_LONG"
    default_message = "That request is too long."


class PhotoTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "AGENT_PHOTO_TOO_LARGE"
    default_message = "That photo is too large."


class PhotoTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "AGENT_PHOTO_TYPE"
    default_message = "That isn't a photo this app can read."


class SkillTakesNoPhotosError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "AGENT_SKILL_TAKES_NO_PHOTOS"
    default_message = "This skill doesn't read photos."


# The body is the photo itself (no form encoding): read into memory, never onto disk.
PHOTO_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            kind: {"schema": {"type": "string", "format": "binary"}} for kind in SIGNATURES
        },
    }
}


def get_runtime(request: Request) -> AgentRuntime:
    return cast(AgentRuntime, request.app.state.agents)


RuntimeDep = Annotated[AgentRuntime, Depends(get_runtime)]


@router.get("", summary="Agents, and whether AI can run for me", responses=error_responses(401))
async def list_agents(runtime: RuntimeDep, ctx: ActorDep, params: PageParamsDep) -> Page[AgentView]:
    catalog, user = runtime.catalog, ctx.user
    remaining = await runtime.quota.remaining(user.id) if user.id else 0
    for_photos = await runtime.blocker(user, vision=True)
    # As a run does: only the skills of modules this account may use (a module given to some
    # accounts only isn't there at all for the others).
    usable = await ModuleService.of(ctx).usable_owners()
    views = []
    for agent in catalog.agents.values():
        availability = await runtime.availability(agent, user)
        skills = [
            SkillView(
                name=s.name,
                description=s.description,
                mode=runtime.settings.mode_for(s.name),
                has_static=s.static is not None,
                photo_mode=runtime.plan_for(s, for_photos).mode if "photo" in s.reads else None,
            )
            for s in catalog.skills_of(agent)
            if usable(s.owner)
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
    responses=error_responses(401, 403, 404, 422, 429),
)
async def run_agent(name: str, body: RunIn, runtime: RuntimeDep, ctx: ActorDep) -> RunOut:
    settings, skill = runtime.settings, runtime.catalog.skills.get(body.skill or "")
    # Text read from a photo is longer than anything typed: allowed only for a skill reading it.
    ocr = body.context.input_type == "ocr" and skill is not None and "ocr" in skill.reads
    limit = settings.max_ocr_chars if ocr else settings.max_input_chars
    if len(body.input) > limit:
        raise InputTooLongError(details={"max": limit})
    request = RunRequest(
        agent=name,
        input=body.input,
        skill=body.skill,
        locale=body.locale or ctx.user.locale,
        context=body.context.model_dump(),
    )
    return RunOut.of(await runtime.run(request, ctx))


@router.post(
    "/{name}/photo-runs",
    status_code=status.HTTP_201_CREATED,
    summary="Ask an agent's skill to read a photo (a receipt)",
    description="For a skill that reads photos (its metadata `reads`). The photo is held in "
    "memory for the run and sent only to providers that read images; it is never stored. "
    "Without AI the run falls to the skill's static script, which reads text only: the app then "
    "reads the photo on the device and sends that text instead.",
    dependencies=[Depends(rate_limit("agent_run"))],
    responses=error_responses(401, 403, 404, 413, 415, 422, 429),
    openapi_extra=PHOTO_BODY,
)
async def run_agent_on_photo(
    name: str,
    skill: str,
    http: Request,
    runtime: RuntimeDep,
    ctx: ActorDep,
    locale: Locale | None = None,
) -> RunOut:
    # What is asked for first, so an unknown agent or skill is a 404 whatever was sent.
    [chosen] = runtime.catalog.skills_of(runtime.catalog.agent(name), skill)
    if "photo" not in chosen.reads:
        raise SkillTakesNoPhotosError(details={"skill": skill})
    data, settings = await http.body(), runtime.settings
    if len(data) > settings.max_photo_bytes:
        raise PhotoTooLargeError(details={"max_bytes": settings.max_photo_bytes})
    kind = sniff(data)
    if kind is None or kind not in settings.photo_types:
        raise PhotoTypeError(details={"types": list(settings.photo_types)})
    request = RunRequest(
        agent=name,
        input="",
        skill=skill,
        locale=locale or ctx.user.locale,
        context={"modules": [], "input_type": "photo"},  # set here only, never by a client
        photos=(Photo(data, kind),),
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
async def confirm_proposal(proposal_id: str, runtime: RuntimeDep, ctx: ActorDep) -> Proposal:
    return await runtime.resolve(proposal_id, confirm=True, ctx=ctx)


@router.post(
    "/proposals/{proposal_id}/reject",
    summary="Reject a proposed change",
    responses=error_responses(401, 404, 409),
)
async def reject_proposal(proposal_id: str, runtime: RuntimeDep, ctx: ActorDep) -> Proposal:
    return await runtime.resolve(proposal_id, confirm=False, ctx=ctx)
