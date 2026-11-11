"""The agent loop on Strands (ADR-0026): the runtime's rules, model call by model call, with
Strands driving the turns: tools gated by loaded skills, the turn limit, quota per call, photos,
failover, and no telemetry."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from typing import Any

import httpx
import respx
from beanie import PydanticObjectId
from httpx import AsyncClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import PROJECT_ROOT, AgentSettings, ProviderSettings
from app.platform.agents.models import AgentRun
from app.platform.agents.photos import Photo
from app.platform.agents.runtime import RunRequest
from tests.helpers import FrozenClock, actor_of, build_app, build_settings, register, running_client
from tests.platform.agents.conftest import (
    BACKUP,
    BACKUP_URL,
    LLM_URL,
    PRIMARY,
    AgentClient,
    ask,
    calls,
    give_consent,
    reply,
    sent,
)

VISION = ProviderSettings(
    name="vision", base_url="https://vision.test/v1", model="free-vision", vision=True
)
VISION_URL = "https://vision.test/v1/chat/completions"
LOAD = ("load_skill", {"name": "manage-modules"})


def tool_messages(route: Any, index: int) -> list[dict[str, Any]]:
    return [m for m in sent(route, index)["messages"] if m["role"] == "tool"]


async def stored(run: dict[str, Any]) -> AgentRun:
    found = await AgentRun.get(PydanticObjectId(run["id"]))
    assert found is not None
    return found


@respx.mock
async def test_only_loaded_skills_tools_run_and_each_refusal_is_a_step(
    ai_client: AsyncClient,
) -> None:
    route = respx.post(LLM_URL).mock(
        side_effect=[
            calls(("modules_list", {})),  # its skill isn't loaded yet
            calls(LOAD, ("modules_list", "{broken")),
            calls(("modules_list", {}), ("no_such_tool", {})),
            reply("Done."),
        ]
    )
    account = await register(ai_client)
    await give_consent(ai_client, account)

    run = await ask(ai_client, account, "list my modules")
    record = await stored(run)

    assert (run["status"], run["skill"], run["message"]) == ("completed", "manage-modules", "Done.")
    assert [(step.tool, step.ok, step.error) for step in record.steps] == [
        ("modules_list", False, "tool_not_loaded"),
        ("load_skill", True, None),
        ("modules_list", False, "arguments_not_json"),
        ("modules_list", True, None),
        ("no_such_tool", False, "tool_not_loaded"),
    ]
    assert [m["content"] for m in tool_messages(route, 1)] == ['{"error": "tool_not_loaded"}']
    offered = [[tool["function"]["name"] for tool in sent(route, i)["tools"]] for i in range(3)]
    assert offered[0] == ["load_skill"]
    assert "modules_list" not in offered[1]
    assert {"modules_list", "load_skill"} <= set(offered[2])  # other skills remain to load
    # The model's own calls go back with their ids, each answered in order.
    third = sent(route, 2)["messages"]
    assistant = [m for m in third if m["role"] == "assistant"][-1]
    assert [call["id"] for call in assistant["tool_calls"]] == ["call-0", "call-1"]
    assert [m["tool_call_id"] for m in tool_messages(route, 2)][-2:] == ["call-0", "call-1"]
    assert run["stages"] == ["understanding", "reading", "checking", "done"]


@respx.mock
async def test_the_turn_limit_ends_the_run_as_incomplete(agent_client: AgentClient) -> None:
    route = respx.post(LLM_URL).mock(return_value=calls(LOAD))
    async with agent_client(providers=[PRIMARY], max_steps=2) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits")
        record = await stored(run)

    assert (run["status"], run["reason"], run["message"]) == ("incomplete", None, None)
    assert route.call_count == record.calls == 2
    assert [step.tool for step in record.steps] == ["load_skill", "load_skill"]


@respx.mock
async def test_each_model_call_takes_one_call_from_the_quota(agent_client: AgentClient) -> None:
    route = respx.post(LLM_URL).mock(
        side_effect=[
            calls(LOAD),
            calls(("modules_open", {"key": "journal"})),
            reply("never asked"),
        ]
    )
    async with agent_client(providers=[PRIMARY], user_daily_calls=2) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "open my journal")
        agents = (await client.get("/v1/agents", headers=account.headers)).json()["items"]
        record = await stored(run)

    assert route.call_count == record.calls == 2  # the third call had no quota: never sent
    assert agents[0]["quota_remaining"] == 0
    assert (run["status"], run["reason"]) == ("incomplete", "quota")
    assert run["client_actions"] == [{"tool": "modules_open", "arguments": {"key": "journal"}}]


@respx.mock
async def test_photos_go_as_image_parts_after_the_masked_words(
    mongo_uri: str, clock: FrozenClock
) -> None:
    photo = b"\xff\xd8\xff\xe0" + b"receipt" * 50
    vision = respx.post(VISION_URL).mock(
        side_effect=[calls(("expenses_categories", {})), reply("That isn't a receipt.")]
    )
    settings = AgentSettings(_env_file=None, providers=(VISION,), fallback=False)
    app = build_app(build_settings(mongo_uri, agent=settings), clock)
    async with running_client(app) as client:
        account = await register(client)
        await give_consent(client, account)
        request = RunRequest(
            "assistant",
            "my receipt, mail asha@example.com",
            "scan-receipt",
            "en",
            {},
            (Photo(photo, "image/jpeg"),),
        )
        run = await app.state.agents.run(request, await actor_of(app, account))

    image = {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(photo).decode()}"},
    }
    for index in range(vision.call_count):
        messages = sent(vision, index)["messages"]
        [user] = [m for m in messages if m["role"] == "user"]
        assert user["content"] == [{"type": "text", "text": "my receipt, mail [redacted]"}, image]
        # The agent's instructions, then the preloaded skill's.
        assert [m["role"] for m in messages[:3]] == ["system", "system", "user"]
    assert sent(vision, 1)["messages"][-1]["role"] == "tool"
    assert (run.status, run.message, run.calls) == ("completed", "That isn't a receipt.", 2)


@respx.mock
async def test_a_provider_that_fails_hands_over_to_the_next(agent_client: AgentClient) -> None:
    primary = respx.post(LLM_URL).mock(side_effect=[httpx.Response(429)])
    backup = respx.post(BACKUP_URL).mock(side_effect=[calls(LOAD), reply("Done.")])
    async with agent_client(providers=[PRIMARY, BACKUP]) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits")
        record = await stored(run)

    assert (run["status"], run["message"]) == ("completed", "Done.")
    assert primary.call_count == 1  # resting after its 429: the second call skips it
    assert backup.call_count == record.calls == 2
    assert record.provider == "backup"


@respx.mock
async def test_no_opentelemetry_provider_is_set_up(ai_client: AsyncClient) -> None:
    respx.post(LLM_URL).mock(side_effect=[calls(LOAD), reply("Done.")])
    account = await register(ai_client)
    await give_consent(ai_client, account)

    run = await ask(ai_client, account, "pin habits")

    assert run["mode"] == "agent"
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    assert not isinstance(metrics.get_meter_provider(), MeterProvider)
    assert not trace.get_tracer("check").start_span("anything").is_recording()


STARTUP = """
import json, sys
from tests.helpers import build_app, build_settings
build_app(build_settings("mongodb://localhost:27017/tt-startup-only"))
print(json.dumps(sorted({"strands", "boto3"} & set(sys.modules))))
"""


def test_strands_is_not_imported_at_startup() -> None:
    found = subprocess.run(  # noqa: S603 - this interpreter, on code written here
        [sys.executable, "-c", STARTUP],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert json.loads(found.stdout.strip().splitlines()[-1]) == []
