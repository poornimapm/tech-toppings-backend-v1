"""Runs agents (ADR-0013).

1. ``policy.plan`` picks the mode from the switches and from what blocks AI right now.
2. Agent mode: the model sees the agent's instructions and its skills' one-line descriptions,
   loads a skill with ``load_skill`` (progressive disclosure, as in Agent Skills), then calls
   that skill's tools. Static mode: each skill's ``scripts/static.py`` gets a turn, in order.
3. Writes are never executed during a run: they become proposals the user confirms.

No prompt text lives here: agent and skill markdown carry it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.core.config import AgentMode, AgentSettings
from app.core.documents import OwnedDocument
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.repository import ScopedRepository
from app.platform.agents.catalog import (
    AgentCatalog,
    AgentSpec,
    SkillSpec,
    StaticOutcome,
    StaticRequest,
)
from app.platform.agents.models import AgentRun, RunStatus, RunStep
from app.platform.agents.policy import Blocker, Plan, plan
from app.platform.agents.providers import ProviderChain, ProvidersUnavailableError, ToolCall
from app.platform.agents.quota import Quota
from app.platform.agents.tools import Proposal, ProposalStatus, ToolBox
from app.platform.context import ActorContext
from app.platform.modules.errors import ModuleDisabledError
from app.platform.modules.service import ModuleService
from app.platform.users.models import User

logger = get_logger(__name__)

LOAD_SKILL = "load_skill"
PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")
NO_MATCH = "static.no_match"


class ProposalClosedError(ConflictError):
    code = "AGENT_PROPOSAL_CLOSED"
    default_message = "This proposal was already handled or has expired."


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


@dataclass
class _Outcome:
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


class AgentRunRepository(ScopedRepository[AgentRun]):
    model: ClassVar[type[OwnedDocument]] = AgentRun

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

    # ------------------------------------------------------------------ policy

    def redact(self, text: str) -> str:
        for pattern in self._redactions:
            text = pattern.sub("[redacted]", text)
        return text

    async def blocker(self, user: User) -> Blocker | None:
        if user.ai_consent_at is None:
            return Blocker.CONSENT
        if not self.chain.configured:
            return Blocker.NO_PROVIDER
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

    # ------------------------------------------------------------------ runs

    @staticmethod
    def _modules(ctx: ActorContext) -> ModuleService:
        return ModuleService(ctx.state.modules, ctx.scope, ctx.clock, ctx.settings)

    async def run(self, request: RunRequest, ctx: ActorContext) -> AgentRun:
        agent = self.catalog.agent(request.agent)
        # A module's skills serve the user only while that module is available to them.
        usable = await self._modules(ctx).usable_owners()
        skills = [
            skill for skill in self.catalog.skills_of(agent, request.skill) if usable(skill.owner)
        ]
        box = ToolBox(
            self.catalog.tools_of(skills),
            ctx,
            timedelta(minutes=self.settings.proposal_ttl_minutes),
        )
        decision = self._plan(skills, request.skill, await self.blocker(ctx.user))
        outcome = _Outcome(skill=request.skill)
        if decision.mode is AgentMode.AGENT:
            try:
                await self._agent(agent, skills, request, box, ctx, outcome)
            except _Blocked as blocked:  # nothing was produced yet: the switches decide again
                decision = self._plan(skills, request.skill, blocked.blocker)
        if decision.mode is AgentMode.STATIC:
            await self._static(skills, request, ctx, outcome, box)
        if decision.mode is None:
            outcome.status = RunStatus.UNAVAILABLE
        return await self._save(agent, request, ctx, decision, outcome, box)

    def _system_prompt(
        self, agent: AgentSpec, skills: Sequence[SkillSpec], request: RunRequest, user: User
    ) -> str:
        today = self.clock.now().astimezone(ZoneInfo(user.timezone)).date().isoformat()
        values = {
            "skills": "\n".join(f"- {skill.name}: {skill.description}" for skill in skills),
            "name": user.name,
            "locale": request.locale,
            "today": today,
            "context": json.dumps(dict(request.context), ensure_ascii=False),
        }
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), agent.instructions)

    def _definitions(
        self, loaded: Mapping[str, SkillSpec], available: Sequence[str]
    ) -> list[dict[str, Any]]:
        tools = [
            self.catalog.tools[name].definition() for name in self.catalog.tools_of(loaded.values())
        ]
        unloaded = [name for name in available if name not in loaded]
        if unloaded:
            parameters = {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": unloaded}},
                "required": ["name"],
                "additionalProperties": False,
            }
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": LOAD_SKILL,
                        "description": "Load a skill's instructions and tools.",
                        "parameters": parameters,
                    },
                }
            )
        return tools

    async def _dispatch(
        self,
        call: ToolCall,
        loaded: dict[str, SkillSpec],
        skills: Mapping[str, SkillSpec],
        box: ToolBox,
    ) -> dict[str, Any]:
        if call.arguments is None:
            return {"error": "arguments_not_json"}
        if call.name == LOAD_SKILL:
            skill = skills.get(str(call.arguments.get("name")))
            if skill is None:
                return {"error": "unknown_skill"}
            loaded[skill.name] = skill
            return {"instructions": skill.instructions}
        if call.name not in self.catalog.tools_of(loaded.values()):
            return {"error": "tool_not_loaded"}
        return await box.call(call.name, call.arguments)

    async def _agent(
        self,
        agent: AgentSpec,
        skills: list[SkillSpec],
        request: RunRequest,
        box: ToolBox,
        ctx: ActorContext,
        out: _Outcome,
    ) -> None:
        by_name = {skill.name: skill for skill in skills}
        loaded = {
            skill.name: skill for skill in skills if request.skill
        }  # a named skill is preloaded
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(agent, skills, request, ctx.user)}
        ]
        messages += [{"role": "system", "content": skill.instructions} for skill in loaded.values()]
        messages.append({"role": "user", "content": self.redact(request.input)})
        for _ in range(self.settings.max_steps):
            if ctx.user.id is None or not await self.quota.take(ctx.user.id):
                self._stop(out, Blocker.QUOTA)
                return
            try:
                completion = await self.chain.complete(
                    messages, self._definitions(loaded, list(by_name))
                )
            except ProvidersUnavailableError:
                self._stop(out, Blocker.PROVIDERS_FAILED)
                return
            out.calls += 1
            out.provider = completion.provider
            out.prompt_tokens += completion.prompt_tokens
            out.completion_tokens += completion.completion_tokens
            messages.append(completion.message)
            if not completion.tool_calls:
                out.message = completion.content
                return
            for call in completion.tool_calls:
                result = await self._dispatch(call, loaded, by_name, box)
                out.steps.append(
                    RunStep(tool=call.name, ok="error" not in result, error=result.get("error"))
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
            if len(loaded) == 1 and out.skill is None:
                out.skill = next(iter(loaded))
        out.status = RunStatus.INCOMPLETE

    @staticmethod
    def _stop(out: _Outcome, blocker: Blocker) -> None:
        """Blocked before the first answer: let the switches decide. Later: keep what we have."""
        if out.calls == 0:
            raise _Blocked(blocker)
        out.status, out.reason = RunStatus.INCOMPLETE, blocker

    async def _static(
        self,
        skills: list[SkillSpec],
        request: RunRequest,
        ctx: ActorContext,
        out: _Outcome,
        box: ToolBox,
    ) -> None:
        """Each skill's script gets a turn. The first confident answer wins; a tentative one is
        used only if no other skill answers."""
        today = self.clock.now().astimezone(ZoneInfo(ctx.user.timezone)).date()
        fallback: tuple[SkillSpec, StaticOutcome, ToolBox] | None = None
        # The more specific skill first: a module knows its own words ("show my expenses")
        # better than the platform's general skills do.
        modules = {module.manifest.key for module in ctx.state.modules}
        for skill in sorted(skills, key=lambda skill: skill.owner not in modules):
            if skill.static is None:
                continue
            scoped = ToolBox(self.catalog.tools_of([skill]), ctx, box.proposal_ttl)
            references = skill.folder / "references"
            result = await skill.static(
                StaticRequest(request.input, request.locale, today, request.context, references),
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

    async def _save(
        self,
        agent: AgentSpec,
        request: RunRequest,
        ctx: ActorContext,
        decision: Plan,
        out: _Outcome,
        box: ToolBox,
    ) -> AgentRun:
        run = await AgentRunRepository(ctx.scope, self.clock).create(
            {
                "agent": agent.name,
                "skill": out.skill,
                "status": out.status,
                "mode": decision.mode,
                "reason": out.reason or decision.reason,
                "input": self.redact(request.input),
                "locale": request.locale,
                "message": out.message,
                "message_code": out.code,
                "message_params": out.params,
                "proposals": box.proposals,
                "client_actions": box.client_actions,
                "steps": out.steps,
                "provider": out.provider,
                "calls": out.calls,
                "prompt_tokens": out.prompt_tokens,
                "completion_tokens": out.completion_tokens,
                "purge_at": self.clock.now() + timedelta(days=self.settings.run_retention_days),
            }
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
