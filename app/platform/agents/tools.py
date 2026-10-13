"""Tools: typed async functions that agents (and static skill scripts) call.

``@tool("read")`` runs immediately; ``@tool("write")`` never runs during a run but becomes a
proposal the user confirms; ``@tool("client")`` is handed to the UI (open a page ...). The
argument schema comes from the type hints and the description from the docstring, so a tool
is just a function. Names start with the owning package's key (``modules_list``).
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, TypeVar, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic_core import to_jsonable_python

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import AppError
from app.core.repository import Scope
from app.platform.users.models import User

Effect = Literal["read", "write", "client"]
TOOL_ATTRIBUTE = "__agent_tool__"
# Write tools take one extra argument: the agent's own sentence the user confirms.
SUMMARY_ARGUMENT = "summary"
SUMMARY_SCHEMA = {
    "type": "string",
    "description": "One short sentence, in the user's language, describing this change. "
    "It is shown to the user, who confirms or rejects it.",
}

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass(frozen=True)
class ToolContext:
    """Who is acting, and the app's shared services (``state``: module registry, ...)."""

    user: User
    scope: Scope
    clock: Clock
    settings: Settings
    state: Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    effect: Effect
    arguments: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]

    def definition(self) -> dict[str, Any]:
        """OpenAI-compatible function definition (also valid as an MCP input schema)."""
        schema = self.arguments.model_json_schema()
        if self.effect == "write":
            schema = {
                **schema,
                "properties": {**schema.get("properties", {}), SUMMARY_ARGUMENT: SUMMARY_SCHEMA},
                "required": [*schema.get("required", []), SUMMARY_ARGUMENT],
            }
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }

    def parse(self, raw: Mapping[str, Any]) -> BaseModel:
        return self.arguments.model_validate(dict(raw))

    async def run(self, ctx: ToolContext, arguments: BaseModel) -> Any:
        return to_jsonable_python(await self.fn(ctx, **dict(arguments)))


def tool(effect: Effect) -> Callable[[F], F]:
    """Mark an async function ``(ctx: ToolContext, **arguments)`` as an agent tool."""

    def register(fn: F) -> F:
        target: Callable[..., Any] = fn  # keep fn's own type: the check below narrows it
        if not inspect.iscoroutinefunction(target):
            raise TypeError(f"tool {fn.__name__} must be async")
        description = inspect.getdoc(fn)
        if not description:
            raise TypeError(f"tool {fn.__name__} needs a docstring: it is the tool's description")
        hints = get_type_hints(fn, include_extras=True)
        parameters = list(inspect.signature(fn).parameters.values())[1:]  # skip ctx
        fields: dict[str, Any] = {
            p.name: (hints[p.name], ... if p.default is p.empty else p.default) for p in parameters
        }
        arguments = create_model(
            f"{fn.__name__}_arguments", __config__=ConfigDict(extra="forbid"), **fields
        )
        setattr(fn, TOOL_ATTRIBUTE, ToolSpec(fn.__name__, description, effect, arguments, fn))
        return fn

    return register


class ProposalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FAILED = "failed"


class Proposal(BaseModel):
    """A write the agent prepared; nothing happens until the user confirms it."""

    # Responses always carry every field, so clients can rely on them (OpenAPI marks them).
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    tool: str
    arguments: dict[str, Any]
    summary: str | None = None  # agent mode: the agent's sentence
    summary_code: str | None = None  # static mode: translated on the client
    summary_params: dict[str, Any] = Field(default_factory=dict)
    status: ProposalStatus = ProposalStatus.PENDING
    expires_at: datetime
    result: Any = None
    error_code: str | None = None


class ClientAction(BaseModel):
    tool: str
    arguments: dict[str, Any]


@dataclass
class ToolBox:
    """The tools one run may use, with what the run produced (proposals, client actions)."""

    tools: Mapping[str, ToolSpec]
    ctx: ToolContext
    proposal_ttl: timedelta
    proposals: list[Proposal] = field(default_factory=list)
    client_actions: list[ClientAction] = field(default_factory=list)

    def definitions(self) -> list[dict[str, Any]]:
        return [spec.definition() for spec in self.tools.values()]

    def _spec(self, name: str, effect: Effect) -> ToolSpec:
        spec = self.tools.get(name)
        if spec is None or spec.effect != effect:
            raise KeyError(f"{name} is not an allowed {effect} tool")
        return spec

    async def read(self, name: str, raw: Mapping[str, Any]) -> Any:
        spec = self._spec(name, "read")
        return await spec.run(self.ctx, spec.parse(raw))

    async def client(self, name: str, raw: Mapping[str, Any]) -> None:
        spec = self._spec(name, "client")
        arguments = spec.parse(raw)
        await spec.run(self.ctx, arguments)  # validates (e.g. the module exists)
        self.client_actions.append(ClientAction(tool=name, arguments=arguments.model_dump()))

    def propose(
        self,
        name: str,
        raw: Mapping[str, Any],
        *,
        summary: str | None = None,
        code: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Proposal:
        spec = self._spec(name, "write")
        proposal = Proposal(
            tool=name,
            arguments=spec.parse(raw).model_dump(),
            summary=summary,
            summary_code=code,
            summary_params=dict(params or {}),
            expires_at=self.ctx.clock.now() + self.proposal_ttl,
        )
        self.proposals.append(proposal)
        return proposal

    async def call(self, name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Agent path: dispatch by effect. Mistakes go back to the model as data, never raise."""
        spec = self.tools.get(name)
        if spec is None:
            return {"error": "unknown_tool"}
        arguments = dict(raw)
        try:
            if spec.effect == "read":
                return {"result": await self.read(name, arguments)}
            if spec.effect == "client":
                await self.client(name, arguments)
                return {"status": "shown_to_user"}
            summary = arguments.pop(SUMMARY_ARGUMENT, None)
            proposal = self.propose(name, arguments, summary=summary)
        except ValidationError as exc:
            return {
                "error": "invalid_arguments",
                "details": exc.errors(
                    include_url=False, include_context=False, include_input=False
                ),
            }
        except AppError as exc:
            return {"error": exc.code}
        return {"status": "awaiting_user_confirmation", "proposal_id": proposal.id}
