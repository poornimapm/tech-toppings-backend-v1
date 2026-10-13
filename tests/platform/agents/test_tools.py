"""Tools are plain typed functions: schema, dispatch by effect, and errors as data."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import pytest
from beanie import PydanticObjectId
from pydantic import Field

from app.core.errors import NotFoundError
from app.core.repository import Scope
from app.platform.agents.tools import SUMMARY_ARGUMENT, ToolBox, ToolSpec, tool
from app.platform.context import ActorContext
from tests.helpers import FrozenClock


@tool("read")
async def demo_lookup(
    _ctx: ActorContext, key: Annotated[str, Field(description="Which one")], limit: int = 3
) -> dict[str, Any]:
    """Look something up."""
    if key == "missing":
        raise NotFoundError
    return {"key": key, "limit": limit}


@tool("write")
async def demo_change(_ctx: ActorContext, key: str, on: bool) -> dict[str, Any]:
    """Change something."""
    return {"key": key, "on": on}


@tool("client")
async def demo_open(ctx: ActorContext, key: str) -> None:
    """Open something."""


def spec(fn: Any) -> ToolSpec:
    return fn.__agent_tool__  # type: ignore[no-any-return]


@pytest.fixture
def box() -> ToolBox:
    clock = FrozenClock()
    stand_ins: dict[str, Any] = {"user": None, "settings": None, "state": None}  # unused here
    ctx = ActorContext(scope=Scope.for_user(PydanticObjectId()), clock=clock, **stand_ins)
    tools = {s.name: s for s in map(spec, (demo_lookup, demo_change, demo_open))}
    return ToolBox(tools, ctx, timedelta(minutes=5))


def test_schema_comes_from_type_hints_and_docstring() -> None:
    function = spec(demo_lookup).definition()["function"]

    assert function["name"] == "demo_lookup"
    assert function["description"] == "Look something up."
    assert function["parameters"]["required"] == ["key"]
    assert function["parameters"]["properties"]["key"]["description"] == "Which one"
    assert function["parameters"]["additionalProperties"] is False


def test_write_tools_ask_the_agent_for_a_summary() -> None:
    parameters = spec(demo_change).definition()["function"]["parameters"]

    assert SUMMARY_ARGUMENT in parameters["properties"]
    assert parameters["required"] == ["key", "on", SUMMARY_ARGUMENT]


def test_tools_must_be_async_and_documented() -> None:
    with pytest.raises(TypeError, match="async"):
        tool("read")(lambda _ctx: None)  # type: ignore[type-var]

    async def undocumented(ctx: ActorContext) -> None:
        pass

    with pytest.raises(TypeError, match="docstring"):
        tool("read")(undocumented)


async def test_reads_run_at_once(box: ToolBox) -> None:
    assert await box.call("demo_lookup", {"key": "a"}) == {"result": {"key": "a", "limit": 3}}


async def test_writes_become_proposals(box: ToolBox) -> None:
    result = await box.call("demo_change", {"key": "a", "on": True, "summary": "Turn a on"})

    (proposal,) = box.proposals
    assert result == {"status": "awaiting_user_confirmation", "proposal_id": proposal.id}
    assert proposal.arguments == {"key": "a", "on": True}
    assert proposal.summary == "Turn a on"
    assert proposal.expires_at == box.ctx.clock.now() + timedelta(minutes=5)


async def test_client_tools_are_handed_to_the_ui(box: ToolBox) -> None:
    assert await box.call("demo_open", {"key": "a"}) == {"status": "shown_to_user"}
    assert [(a.tool, a.arguments) for a in box.client_actions] == [("demo_open", {"key": "a"})]


@pytest.mark.parametrize(
    ("name", "arguments", "error"),
    [
        ("nope", {}, "unknown_tool"),
        ("demo_lookup", {"key": 1, "extra": True}, "invalid_arguments"),
        ("demo_lookup", {"key": "missing"}, "NOT_FOUND"),
    ],
)
async def test_mistakes_go_back_to_the_model_as_data(
    box: ToolBox, name: str, arguments: dict[str, Any], error: str
) -> None:
    result = await box.call(name, arguments)

    assert result["error"] == error
    assert all("input" not in item for item in result.get("details", []))  # no value echo
    assert box.proposals == []


def test_static_scripts_cannot_use_tools_outside_their_skill(box: ToolBox) -> None:
    with pytest.raises(KeyError):
        box.propose("demo_lookup", {"key": "a"})  # a read tool is not a write tool
