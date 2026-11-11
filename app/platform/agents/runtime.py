"""Runs agents (ADR-0013; the agent loop itself: ADR-0026).

1. ``policy.plan`` picks the mode from the switches and from what blocks AI right now.
2. Agent mode: the model sees the agent's instructions and its skills' one-line descriptions,
   loads a skill with ``load_skill`` (progressive disclosure, as in Agent Skills), then calls
   that skill's tools; Strands runs these turns (``engine.py``, loaded with the first AI run).
   Static mode: each skill's ``scripts/static.py`` gets a turn, in order.
3. Writes are never executed during a run: they become proposals the user confirms.

A run answers when done (``run``), or starts in the background and records its stages for a
page to follow (``start``), which may cancel it (``cancel``). No prompt text lives here: agent
and skill markdown carry it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar

from app.core.clock import Clock
from app.core.config import AgentMode, AgentSettings
from app.core.documents import OwnedDocument
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.repository import Scope, ScopedRepository
from app.platform.agents.catalog import (
    AgentCatalog,
    AgentSpec,
    SkillSpec,
    StaticOutcome,
    StaticRequest,
    UnknownSkillError,
)
from app.platform.agents.models import AgentRun, RunStage, RunStatus, RunStep
from app.platform.agents.photos import Photo
from app.platform.agents.policy import Blocker, Plan, plan
from app.platform.agents.providers import ProviderChain
from app.platform.agents.quota import Quota
from app.platform.agents.tools import Proposal, ProposalStatus, ToolBox
from app.platform.context import ActorContext
from app.platform.modules.errors import ModuleDisabledError
from app.platform.modules.service import ModuleService
from app.platform.users.models import User

logger = get_logger(__name__)

PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")
NO_MATCH = "static.no_match"
ENGINE = "app.platform.agents.engine"  # imports Strands


class ProposalClosedError(ConflictError):
    code = "AGENT_PROPOSAL_CLOSED"
    default_message = "This proposal was already handled or has expired."


class RunFinishedError(ConflictError):
    code = "AGENT_RUN_FINISHED"
    default_message = "This run has already finished."


class _Blocked(Exception):  # noqa: N818 - control flow inside the runtime, never escapes
    def __init__(self, blocker: Blocker) -> None:
        self.blocker = blocker


@dataclass(frozen=True)
class RunRequest:
    agent: str
    input: str
    skill: str | None
    locale: str
    context: Mapping[str, Any]
    photos: tuple[Photo, ...] = ()  # for a provider that reads images; never stored


@dataclass
class Outcome:
    """What a run has made so far (the agent loop in engine.py writes to it too)."""

    status: RunStatus = RunStatus.COMPLETED
    skill: str | None = None
    message: str | None = None
    code: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    steps: list[RunStep] = field(default_factory=list)
    provider: str | None = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reason: Blocker | None = None
    cancelled: bool = False  # the person stopped it while it ran (``AgentRuntime.cancel``)
    plan: Plan = field(default_factory=lambda: Plan(None, None))  # the mode it runs in
    stages: list[RunStage] = field(default_factory=lambda: [RunStage.UNDERSTANDING])
    # A background run's record follows its stages as they come.
    report: Callable[[list[RunStage]], Awaitable[None]] | None = None

    async def stage(self, stage: RunStage) -> None:
        if self.stages[-1] is not stage:
            self.stages.append(stage)
            if self.report is not None:
                await self.report(self.stages)


class AgentRunRepository(ScopedRepository[AgentRun]):
    model: ClassVar[type[OwnedDocument]] = AgentRun

    async def write(self, run: AgentRun, changes: Mapping[str, Any]) -> None:
        """Set fields of a run still being worked on (a background run's stages): once it has
        ended (finished, or cancelled), nothing more is written over it."""
        await AgentRun.get_pymongo_collection().update_one(
            self.query({"_id": run.id, "status": RunStatus.RUNNING}),
            {
                "$set": {
                    **changes,
                    "updated_at": self.clock.now(),
                    "updated_by": self.scope.actor_id,
                }
            },
        )

    async def finish(self, run: AgentRun, fields: Mapping[str, Any]) -> AgentRun:
        """A background run's outcome, checked as a new run is, over its running record."""
        built = self.build(fields)
        await self.write(run, built.model_dump(include=set(fields)))
        done: AgentRun = built.model_copy(update={"id": run.id, "created_at": run.created_at})
        return done

    async def with_proposal(self, proposal_id: str) -> AgentRun:
        run = await AgentRun.find_one(self.query({"proposals.id": proposal_id}))
        if run is None:
            raise NotFoundError
        return run

    async def set_proposal(
        self,
        run: AgentRun,
        proposal_id: str,
        changes: Mapping[str, Any],
        *,
        only_if: ProposalStatus | None,
    ) -> bool:
        match: dict[str, Any] = {"id": proposal_id} | ({"status": only_if} if only_if else {})
        result = await AgentRun.get_pymongo_collection().update_one(
            self.query({"_id": run.id, "proposals": {"$elemMatch": match}}),
            {
                "$set": {f"proposals.$.{key}": value for key, value in changes.items()}
                | {"updated_at": self.clock.now(), "updated_by": self.scope.actor_id}
            },
        )
        return result.modified_count == 1


class AgentRuntime:
    def __init__(
        self, catalog: AgentCatalog, chain: ProviderChain, settings: AgentSettings, clock: Clock
    ) -> None:
        self.catalog = catalog
        self.chain = chain
        self.settings = settings
        self.clock = clock
        self.quota = Quota(settings, clock)
        self._redactions = [re.compile(pattern) for pattern in settings.redact_patterns]
        # Background runs by id, held until done so none is collected mid-run; and those still
        # working (not yet saving their outcome), which ``cancel`` may stop.
        self._background: dict[str, asyncio.Task[None]] = {}
        self._working: set[str] = set()

    # ------------------------------------------------------------------ policy

    def redact(self, text: str) -> str:
        for pattern in self._redactions:
            text = pattern.sub("[redacted]", text)
        return text

    async def blocker(self, user: User, *, vision: bool = False) -> Blocker | None:
        """What keeps AI from this user now; ``vision``: for a run with photos."""
        if user.ai_consent_at is None:
            return Blocker.CONSENT
        if not self.chain.configured(vision=vision):
            return Blocker.NO_PROVIDER
        if not self.chain.ready(vision=vision):
            return Blocker.PROVIDERS_FAILED
        if user.id is not None and await self.quota.remaining(user.id) <= 0:
            return Blocker.QUOTA
        return None

    def _plan(
        self, skills: Sequence[SkillSpec], skill: str | None, blocker: Blocker | None
    ) -> Plan:
        configured = self.settings.mode_for(skill) if skill else self.settings.mode
        has_static = any(item.static for item in skills)
        return plan(configured, blocker, fallback=self.settings.fallback, has_static=has_static)

    async def availability(self, agent: AgentSpec, user: User) -> Plan:
        return self._plan(self.catalog.skills_of(agent), None, await self.blocker(user))

    def plan_for(self, skill: SkillSpec, blocker: Blocker | None) -> Plan:
        """The mode a run of this one skill would use, given what blocks AI."""
        return self._plan([skill], skill.name, blocker)

    # ------------------------------------------------------------------ runs

    @staticmethod
    def _modules(ctx: ActorContext) -> ModuleService:
        return ModuleService.of(ctx)

    async def run(self, request: RunRequest, ctx: ActorContext) -> AgentRun:
        agent, skills = await self._skills(request, ctx)
        out, box = Outcome(skill=request.skill), self._box(skills, ctx)
        await self._work(agent, skills, request, ctx, out, box)
        return await self._save(agent, request, ctx, out, box)

    async def start(self, request: RunRequest, ctx: ActorContext) -> AgentRun:
        """``run`` in the background: answers at once with the run saved as running. Its record
        follows the run's stages, gets the outcome at the end, and ends as incomplete (reason
        timeout) after AGENT_RUN_TIMEOUT_SECONDS, or when the person cancels it."""
        agent, skills = await self._skills(request, ctx)  # a refusal answers at once
        out, box = Outcome(skill=request.skill), self._box(skills, ctx)
        repository = AgentRunRepository(ctx.scope, self.clock)
        running = await repository.create(
            self._fields(agent, request, out, box) | {"status": RunStatus.RUNNING}
        )
        run_id = str(running.id)

        async def report(stages: list[RunStage]) -> None:
            await repository.write(running, {"stages": stages})

        async def work() -> None:
            try:
                async with asyncio.timeout(self.settings.run_timeout_seconds):
                    await self._work(agent, skills, request, ctx, out, box)
            except TimeoutError:
                out.status, out.reason = RunStatus.INCOMPLETE, Blocker.TIMEOUT
            except asyncio.CancelledError:
                if run_id in self._working:  # the server stopping: it reads as timed out
                    raise
                if (current := asyncio.current_task()) is not None:
                    current.uncancel()  # taken on purpose: the save below waits as usual
                logger.info("agent_run_cancelled", run_id=run_id)
                out.status, out.cancelled = RunStatus.INCOMPLETE, True
            except Exception as fault:  # the database ... must not leave it running
                # Its type, not its words or traceback: they may carry what logs must not.
                failed = type(fault).__name__
                logger.error("agent_run_failed", run_id=run_id, error=failed)  # noqa: TRY400
                out.status = RunStatus.INCOMPLETE
            finally:
                self._working.discard(run_id)
            await self._save(agent, request, ctx, out, box, running)

        out.report = report
        task = asyncio.create_task(work())
        self._background[run_id] = task
        self._working.add(run_id)
        task.add_done_callback(lambda _: self._background.pop(run_id, None))
        return running

    async def cancel(self, run_id: str, scope: Scope) -> AgentRun:
        """Stop one of the caller's running runs: it makes no further model call, and is saved
        as incomplete with what it made so far (``cancelled``, reason None). One cancelled
        already answers as it is; one finished otherwise is refused."""
        repository = AgentRunRepository(scope, self.clock)
        run = self.current(await repository.get(run_id))  # another person's: not found
        if run.status is not RunStatus.RUNNING:
            if run.cancelled:
                return run
            raise RunFinishedError(details={"status": run.status})
        key = str(run.id)
        task = self._background.get(key)
        if task is not None:
            if key in self._working:  # not yet saving what it finished with
                self._working.discard(key)
                task.cancel()
            await asyncio.wait([task])
        # Not running here (a restart cut it short) or stopped before it began: ended as it stands.
        ended = {"status": RunStatus.INCOMPLETE, "cancelled": True}
        await repository.write(run, ended | {"stages": [*run.stages, RunStage.DONE]})
        return self.current(await repository.get(run_id))

    def current(self, run: AgentRun) -> AgentRun:
        """A run as it stands: one still running past AGENT_RUN_TIMEOUT_SECONDS (the server
        stopped during it) has timed out."""
        timeout = timedelta(seconds=self.settings.run_timeout_seconds)
        if run.status is RunStatus.RUNNING and run.created_at + timeout <= self.clock.now():
            ended: AgentRun = run.model_copy(
                update={"status": RunStatus.INCOMPLETE, "reason": Blocker.TIMEOUT}
            )
            return ended
        return run

    async def close(self) -> None:
        """At shutdown: stop the background runs (each then reads as timed out)."""
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _skills(
        self, request: RunRequest, ctx: ActorContext
    ) -> tuple[AgentSpec, list[SkillSpec]]:
        agent = self.catalog.agent(request.agent)
        # A module's skills serve the user only while that module is available to them.
        usable = await self._modules(ctx).usable_owners()
        skills = [
            skill for skill in self.catalog.skills_of(agent, request.skill) if usable(skill.owner)
        ]
        if request.skill and not skills:  # asked for by name, but its module is switched off
            raise ModuleDisabledError(details={"module": self.catalog.skills[request.skill].owner})
        return agent, skills

    def _box(self, skills: Sequence[SkillSpec], ctx: ActorContext) -> ToolBox:
        ttl = timedelta(minutes=self.settings.proposal_ttl_minutes)
        return ToolBox(self.catalog.tools_of(skills), ctx, ttl)

    async def _work(
        self,
        agent: AgentSpec,
        skills: list[SkillSpec],
        request: RunRequest,
        ctx: ActorContext,
        out: Outcome,
        box: ToolBox,
    ) -> None:
        """Plan the mode and run in it: all a run does before it is saved."""
        blocker = await self.blocker(ctx.user, vision=bool(request.photos))
        out.plan = self._plan(skills, request.skill, blocker)
        if out.plan.mode is AgentMode.AGENT:
            try:
                await self._agent(agent, skills, request, box, ctx, out)
            except _Blocked as blocked:  # nothing was produced yet: the switches decide again
                out.plan = self._plan(skills, request.skill, blocked.blocker)
        if out.plan.mode is AgentMode.STATIC:
            await self._static(skills, request, ctx, out, box)
        if out.plan.mode is None:
            out.status = RunStatus.UNAVAILABLE

    def _system_prompt(
        self, agent: AgentSpec, skills: Sequence[SkillSpec], request: RunRequest, ctx: ActorContext
    ) -> str:
        values = {
            "skills": "\n".join(f"- {skill.name}: {skill.description}" for skill in skills),
            "name": ctx.user.name,
            "locale": request.locale,
            "today": ctx.today().isoformat(),
            "context": json.dumps(dict(request.context), ensure_ascii=False),
        }
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), agent.instructions)

    async def _agent(
        self,
        agent: AgentSpec,
        skills: list[SkillSpec],
        request: RunRequest,
        box: ToolBox,
        ctx: ActorContext,
        out: Outcome,
    ) -> None:
        # Strands loads with the first AI run, not at startup (~2.4 s), and in a worker thread:
        # on the event loop, every request in flight would wait for it.
        await asyncio.to_thread(importlib.import_module, ENGINE)
        from app.platform.agents.engine import AgentLoop, Stopped  # noqa: PLC0415

        loop = AgentLoop(self, skills, request, box, ctx, out)
        try:
            await loop.run(self._system_prompt(agent, skills, request, ctx))
        except Stopped as stopped:
            self._stop(out, box, stopped.blocker)

    @staticmethod
    def _stop(out: Outcome, box: ToolBox, blocker: Blocker) -> None:
        """Blocked before anything was produced (only reads so far, as when a scan's first
        call fetched the categories): let the switches decide, so a photo is read on the device.
        After something was: keep what we have."""
        if not box.proposals and not box.client_actions:
            raise _Blocked(blocker)
        out.status, out.reason = RunStatus.INCOMPLETE, blocker

    async def _static(
        self,
        skills: list[SkillSpec],
        request: RunRequest,
        ctx: ActorContext,
        out: Outcome,
        box: ToolBox,
    ) -> None:
        """Each skill's script gets a turn. The first confident answer wins; a tentative one is
        used only if no other skill answers."""
        today = ctx.today()
        fallback: tuple[SkillSpec, StaticOutcome, ToolBox] | None = None
        # The more specific skill first: a module knows its own words ("show my expenses")
        # better than the platform's general skills do.
        modules = {module.manifest.key for module in ctx.state.modules}
        for skill in sorted(skills, key=lambda skill: skill.owner not in modules):
            if skill.static is None:
                continue
            scoped = ToolBox(self.catalog.tools_of([skill]), ctx, box.proposal_ttl)
            references = skill.folder / "references"
            chosen = request.skill == skill.name
            result = await skill.static(
                StaticRequest(
                    request.input, request.locale, today, request.context, references, chosen
                ),
                scoped,
            )
            if result is None:
                continue
            answer = (skill, result, scoped)
            if not result.tentative:
                fallback = answer
                break
            fallback = fallback or answer
        if fallback is None:
            out.code = NO_MATCH
            return
        skill, result, scoped = fallback
        out.skill, out.code, out.params = skill.name, result.code, dict(result.params)
        box.proposals += scoped.proposals
        box.client_actions += scoped.client_actions

    def _fields(
        self, agent: AgentSpec, request: RunRequest, out: Outcome, box: ToolBox
    ) -> dict[str, Any]:
        return {
            "agent": agent.name,
            "skill": out.skill,
            "status": out.status,
            "mode": out.plan.mode,
            "reason": out.reason or out.plan.reason,
            "cancelled": out.cancelled,
            "input": self.redact(request.input),
            "photos": len(request.photos),  # how many, never what
            "locale": request.locale,
            "message": out.message,
            "message_code": out.code,
            "message_params": out.params,
            "proposals": box.proposals,
            "client_actions": box.client_actions,
            "steps": out.steps,
            "stages": out.stages,
            "provider": out.provider,
            "calls": out.calls,
            "prompt_tokens": out.prompt_tokens,
            "completion_tokens": out.completion_tokens,
            "purge_at": self.clock.now() + timedelta(days=self.settings.run_retention_days),
        }

    async def _save(
        self,
        agent: AgentSpec,
        request: RunRequest,
        ctx: ActorContext,
        out: Outcome,
        box: ToolBox,
        running: AgentRun | None = None,
    ) -> AgentRun:
        """The finished run: a new record, or over a background run's running one."""
        out.stages.append(RunStage.DONE)
        repository = AgentRunRepository(ctx.scope, self.clock)
        fields = self._fields(agent, request, out, box)
        run = await (
            repository.create(fields) if running is None else repository.finish(running, fields)
        )
        logger.info(
            "agent_run",
            run_id=str(run.id),
            agent=agent.name,
            skill=run.skill,
            mode=run.mode,
            status=run.status,
            reason=run.reason,
            calls=run.calls,
            tools=[step.tool for step in run.steps],
            proposals=len(run.proposals),
        )
        return run

    # ------------------------------------------------------------------ proposals

    async def resolve(self, proposal_id: str, *, confirm: bool, ctx: ActorContext) -> Proposal:
        repository = AgentRunRepository(ctx.scope, self.clock)
        run = await repository.with_proposal(proposal_id)
        proposal = next(item for item in run.proposals if item.id == proposal_id)
        if proposal.status is not ProposalStatus.PENDING or proposal.expires_at <= self.clock.now():
            raise ProposalClosedError(details={"status": proposal.status})
        status = ProposalStatus.CONFIRMED if confirm else ProposalStatus.REJECTED
        if not await repository.set_proposal(
            run, proposal_id, {"status": status}, only_if=ProposalStatus.PENDING
        ):
            raise ProposalClosedError  # another tab got there first
        changes: dict[str, Any] = {"status": status}
        if confirm:
            spec = self.catalog.tools[proposal.tool]
            try:
                # The module may have been switched off since the proposal was made.
                owner = self.catalog.tool_owners[proposal.tool]
                if not (await self._modules(ctx).usable_owners())(owner):
                    raise ModuleDisabledError(details={"module": owner})
                changes["result"] = await spec.run(ctx, spec.parse(proposal.arguments))
            except AppError as exc:
                changes |= {"status": ProposalStatus.FAILED, "error_code": exc.code}
            await repository.set_proposal(run, proposal_id, changes, only_if=None)
        return proposal.model_copy(update=changes)


def _named(
    runtime: AgentRuntime, skill: str, text: str, locale: str, context: Mapping[str, Any] | None
) -> RunRequest:
    """A run of one skill, through the first agent that lists it."""
    agent = next((spec for spec in runtime.catalog.agents.values() if skill in spec.skills), None)
    if agent is None:
        raise UnknownSkillError(details={"skill": skill})
    return RunRequest(agent.name, text, skill, locale, dict(context or {}))


async def run_skill(
    ctx: ActorContext,
    skill: str,
    text: str,
    *,
    locale: str,
    context: Mapping[str, Any] | None = None,
) -> AgentRun:
    """Run one skill for the caller, as a page's own button would: by the same switches,
    quota and consent as any run, and recorded the same way. For module code that keeps its
    own cache in front (insights), so an unchanged answer costs no AI call."""
    runtime: AgentRuntime = ctx.state.agents
    return await runtime.run(_named(runtime, skill, text, locale, context), ctx)


async def start_skill(
    ctx: ActorContext,
    skill: str,
    text: str,
    *,
    locale: str,
    context: Mapping[str, Any] | None = None,
) -> AgentRun:
    """``run_skill`` in the background, for a page that shows the run's progress: answers at
    once with the run saved as ``running``. ``GET /v1/agents/runs/{id}`` then gives its stages
    and, when it is done, its outcome (a draft as a client action)."""
    runtime: AgentRuntime = ctx.state.agents
    return await runtime.start(_named(runtime, skill, text, locale, context), ctx)
