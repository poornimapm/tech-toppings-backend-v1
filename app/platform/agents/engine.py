"""The agent loop, run by Strands (ADR-0026, in place of ADR-0013's own loop).

Imported with the first AI run, never at startup: Strands brings boto3, OpenTelemetry and mcp
(some 55 MB and 2.4 s). Strands drives the turns; the rest stays the runtime's:

- its model is the app's provider chain (configured order, rest after a 429, photos only to
  providers that read images), behind one quota call per model call, in OpenAI format;
- each tool is a Strands tool calling ``ToolBox.call``, so writes still become proposals;
- skills load through the runtime's own ``load_skill``: the model is offered the tools of
  loaded skills only, and a hook refuses any other call;
- no console callback, no retries (the chain fails over), one tool at a time, no conversation
  manager, and ``AGENT_MAX_STEPS`` as Strands' turn limit.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel
from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.hooks import AfterToolsEvent, BeforeToolCallEvent
from strands.models import Model
from strands.tools import PythonAgentTool
from strands.tools.executors import SequentialToolExecutor
from strands.types.content import ContentBlock, Message, Messages, SystemContentBlock
from strands.types.media import ImageContent, ImageFormat
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolResult, ToolSpec, ToolUse

from app.core.config import PhotoType
from app.platform.agents.models import RunStage, RunStatus, RunStep
from app.platform.agents.photos import Photo
from app.platform.agents.policy import Blocker
from app.platform.agents.providers import Completion, ProvidersUnavailableError

if TYPE_CHECKING:
    from app.platform.agents.catalog import SkillSpec
    from app.platform.agents.runtime import AgentRuntime, Outcome, RunRequest
    from app.platform.agents.tools import ToolBox
    from app.platform.context import ActorContext

LOAD_SKILL = "load_skill"
FORMATS: dict[PhotoType, ImageFormat] = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
}
MEDIA_TYPES: dict[ImageFormat, PhotoType] = {kind: media for media, kind in FORMATS.items()}
# What the run is doing while a tool of each effect runs.
TOOL_STAGES = {"read": RunStage.READING, "client": RunStage.DRAFTING, "write": RunStage.PROPOSING}

# A model call the quota or the providers stopped: an empty answer, which ends Strands' loop
# quietly (raised through Strands, it would be logged as a failure and wrapped).
ENDED: tuple[StreamEvent, ...] = (
    {"messageStart": {"role": "assistant"}},
    {"messageStop": {"stopReason": "end_turn"}},
)

T = TypeVar("T", bound=BaseModel)


class Stopped(Exception):  # noqa: N818 - the quota or the providers ended the loop
    def __init__(self, blocker: Blocker) -> None:
        super().__init__(blocker)
        self.blocker = blocker


# ---------------------------------------------------------------------- OpenAI format


def to_openai(
    system: Sequence[SystemContentBlock] | None, messages: Messages
) -> list[dict[str, Any]]:
    """Strands' conversation as chat-completions messages: each system block, then each turn."""
    converted: list[dict[str, Any]] = [
        {"role": "system", "content": block["text"]} for block in system or () if "text" in block
    ]
    for message in messages:
        converted += _turn(message)
    return converted


def _turn(message: Message) -> list[dict[str, Any]]:
    blocks = message["content"]
    results = [block["toolResult"] for block in blocks if "toolResult" in block]
    if results:  # one tool message per result, in the order of the calls
        return [
            {
                "role": "tool",
                "tool_call_id": result["toolUseId"],
                "content": "".join(item.get("text", "") for item in result["content"]),
            }
            for result in results
        ]
    text = "".join(block["text"] for block in blocks if "text" in block)
    if message["role"] == "assistant":
        calls = [_call(block["toolUse"]) for block in blocks if "toolUse" in block]
        return [
            {"role": "assistant", "content": text or None}
            | ({"tool_calls": calls} if calls else {})
        ]
    images = [_image(block["image"]) for block in blocks if "image" in block]
    if not images:
        return [{"role": "user", "content": text}]
    words = [{"type": "text", "text": text}] if text else []
    return [{"role": "user", "content": [*words, *images]}]


def _call(use: ToolUse) -> dict[str, Any]:
    arguments = json.dumps(use["input"], ensure_ascii=False)
    return {
        "id": use["toolUseId"],
        "type": "function",
        "function": {"name": use["name"], "arguments": arguments},
    }


def _image(image: ImageContent) -> dict[str, object]:
    return Photo(image["source"]["bytes"], MEDIA_TYPES[image["format"]]).part()


def prompt(text: str, photos: Sequence[Photo]) -> list[ContentBlock]:
    """The user's turn: the (masked) words, and then each photo; with photos, words only if any."""
    words: list[ContentBlock] = [{"text": text}] if text.strip() or not photos else []
    images: list[ContentBlock] = [
        {"image": {"format": FORMATS[photo.media_type], "source": {"bytes": photo.data}}}
        for photo in photos
    ]
    return [*words, *images]


def events(completion: Completion) -> Iterator[StreamEvent]:
    """A provider's whole answer as the stream Strands reads (the chain does not stream)."""
    yield {"messageStart": {"role": "assistant"}}
    if completion.content:
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": completion.content}}}
        yield {"contentBlockStop": {}}
    for call in completion.tool_calls:
        arguments = json.dumps(call.arguments or {}, ensure_ascii=False)  # None: see ``broken``
        yield {
            "contentBlockStart": {"start": {"toolUse": {"toolUseId": call.id, "name": call.name}}}
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": arguments}}}}
        yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": "tool_use" if completion.tool_calls else "end_turn"}}
    used = completion.prompt_tokens, completion.completion_tokens
    yield {
        "metadata": {
            "usage": {"inputTokens": used[0], "outputTokens": used[1], "totalTokens": sum(used)},
            "metrics": {"latencyMs": 0},
        }
    }


def _strands_spec(definition: Mapping[str, Any]) -> ToolSpec:
    function = definition["function"]
    return {
        "name": function["name"],
        "description": function["description"],
        "inputSchema": {"json": function["parameters"]},
    }


def load_skill_definition(names: Sequence[str]) -> dict[str, Any]:
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": list(names)}},
        "required": ["name"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": LOAD_SKILL,
            "description": "Load a skill's instructions and tools.",
            "parameters": parameters,
        },
    }


# ---------------------------------------------------------------------- the loop


class ChainModel(Model):
    """Strands' model: the loop's ``complete`` (one quota call, then the provider chain)."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    def update_config(self, **model_config: Any) -> None:
        """Nothing to set: each provider's settings come from AGENT_PROVIDERS."""

    def get_config(self) -> dict[str, Any]:
        return {}

    def structured_output(
        self,
        output_model: type[T],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        raise NotImplementedError  # skills answer through tools, never Strands' structured output

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,  # noqa: ARG002 - the loop offers its own
        system_prompt: str | None = None,  # noqa: ARG002 - the blocks below carry it
        *,
        system_prompt_content: list[SystemContentBlock] | None = None,
        **kwargs: Any,  # noqa: ARG002 - Strands' extras (cancel signal ...) need nothing here
    ) -> AsyncGenerator[StreamEvent, None]:
        completion = await self.loop.complete(to_openai(system_prompt_content, messages))
        for event in ENDED if completion is None else events(completion):
            yield event


class AgentLoop:
    """One agent-mode run: a Strands agent built for it, over the runtime's own pieces."""

    def __init__(
        self,
        runtime: AgentRuntime,
        skills: Sequence[SkillSpec],
        request: RunRequest,
        box: ToolBox,
        ctx: ActorContext,
        out: Outcome,
    ) -> None:
        self.runtime, self.request, self.box, self.ctx, self.out = runtime, request, box, ctx, out
        self.skills = {skill.name: skill for skill in skills}
        # A skill named in the request is loaded from the start.
        self.loaded = {skill.name: skill for skill in skills if request.skill}
        self.broken: set[str] = set()  # the last answer's calls whose arguments weren't JSON
        self.last: Completion | None = None
        self.stopped: Blocker | None = None
        # A tool's unexpected failure (the database gone ...): it ends the loop, and is raised
        # once Strands returns, so the run ends as it did before Strands.
        self.fault: Exception | None = None

    async def run(self, instructions: str) -> None:
        """``instructions``: the agent's own, filled in; a preloaded skill's follow them.
        Raises ``Stopped`` when the quota or the providers ended the loop, and a tool's
        unexpected failure as it was raised."""
        system: list[SystemContentBlock] = [{"text": instructions}]
        system += [{"text": skill.instructions} for skill in self.loaded.values()]
        agent = Agent(
            model=ChainModel(self),
            tools=[
                PythonAgentTool(
                    definition["function"]["name"], _strands_spec(definition), self._call
                )
                for definition in self._reachable()
            ],
            system_prompt=system,
            callback_handler=None,
            conversation_manager=NullConversationManager(),
            tool_executor=SequentialToolExecutor(),
            retry_strategy=None,
        )
        agent.add_hook(self._gate, BeforeToolCallEvent)
        agent.add_hook(self._after_tools, AfterToolsEvent)
        text = self.runtime.redact(self.request.input)
        result = await agent.invoke_async(
            prompt(text, self.request.photos), limits={"turns": self.runtime.settings.max_steps}
        )
        if self.fault is not None:
            raise self.fault
        if self.stopped is not None:
            raise Stopped(self.stopped)
        if result.stop_reason == "limit_turns":
            self.out.status = RunStatus.INCOMPLETE
        elif self.last is not None:
            self.out.message = self.last.content

    async def complete(self, messages: list[dict[str, Any]]) -> Completion | None:
        """One model call: one call from the day's quota, then the provider chain. None: one of
        them stopped the loop (``stopped`` says which), or a tool failed (``fault``)."""
        if self.fault is not None:
            return None
        if self.out.stages[-1] in TOOL_STAGES.values():
            await self.out.stage(RunStage.CHECKING)
        user = self.ctx.user.id
        if user is None or not await self.runtime.quota.take(user):
            self.stopped = Blocker.QUOTA
            return None
        budgets = [skill.max_tokens for skill in self.loaded.values() if skill.max_tokens]
        try:
            completion = await self.runtime.chain.complete(
                messages,
                self._offered(),
                vision=bool(self.request.photos),
                max_tokens=max(budgets, default=None),
            )
        except ProvidersUnavailableError:
            self.stopped = Blocker.PROVIDERS_FAILED
            return None
        out = self.out
        out.calls += 1
        out.provider = completion.provider
        out.prompt_tokens += completion.prompt_tokens
        out.completion_tokens += completion.completion_tokens
        # This turn's: ids need only be unique within one answer.
        self.broken = {call.id for call in completion.tool_calls if call.arguments is None}
        self.last = completion
        return completion

    def _offered(self) -> list[dict[str, Any]]:
        """Progressive disclosure: the loaded skills' tools, and load_skill while any remain."""
        catalog = self.runtime.catalog
        tools = [spec.definition() for spec in catalog.tools_of(self.loaded.values()).values()]
        unloaded = [name for name in self.skills if name not in self.loaded]
        return [*tools, load_skill_definition(unloaded)] if unloaded else tools

    def _reachable(self) -> list[dict[str, Any]]:
        """Every tool the run could reach, for Strands to call; ``_offered`` says which now."""
        catalog = self.runtime.catalog
        tools = [spec.definition() for spec in catalog.tools_of(self.skills.values()).values()]
        return [*tools, load_skill_definition(list(self.skills))]

    def _gate(self, event: BeforeToolCallEvent) -> None:
        """Refuse what the model may not call now: arguments that weren't JSON, or a tool of a
        skill not loaded (an unknown tool among them). The refusal goes back as its result."""
        use = event.tool_use
        loaded = self.runtime.catalog.tools_of(self.loaded.values())
        if use["toolUseId"] in self.broken:
            event.cancel_tool = self._answer(use, {"error": "arguments_not_json"})
        elif use["name"] != LOAD_SKILL and use["name"] not in loaded:
            event.cancel_tool = self._answer(use, {"error": "tool_not_loaded"})

    async def _call(self, use: ToolUse, **_: Any) -> ToolResult:
        name, arguments = use["name"], use["input"]
        if name == LOAD_SKILL:
            result = self._load(arguments)
        elif self.fault is not None:  # an earlier call of this answer failed: the run is over
            result = {"error": "tool_skipped"}
        else:
            # Strands would send the failure's own words to the provider and carry on; only
            # Exception, so a cancel or the run's timeout still goes through.
            try:
                await self.out.stage(TOOL_STAGES[self.runtime.catalog.tools[name].effect])
                result = await self.box.call(name, arguments)
            except Exception as fault:
                self.fault, result = fault, {"error": "tool_failed"}
        return {
            "toolUseId": use["toolUseId"],
            "status": "error" if "error" in result else "success",
            "content": [{"text": self._answer(use, result)}],
        }

    def _load(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        skill = self.skills.get(str(arguments.get("name")))
        if skill is None:
            return {"error": "unknown_skill"}
        self.loaded[skill.name] = skill
        return {"instructions": skill.instructions}

    def _answer(self, use: ToolUse, result: Mapping[str, Any]) -> str:
        """Record the step; the result as the model reads it."""
        self.out.steps.append(
            RunStep(tool=use["name"], ok="error" not in result, error=result.get("error"))
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    def _after_tools(self, event: AfterToolsEvent) -> None:  # noqa: ARG002 - the hook's signature
        if len(self.loaded) == 1 and self.out.skill is None:
            self.out.skill = next(iter(self.loaded))
