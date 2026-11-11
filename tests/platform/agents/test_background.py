"""Background runs (``start_skill``): the record is saved as running at once, follows the run's
stages for a page to poll, gets the outcome at the end, and times out, after a restart too. The
person may cancel one; a tool that fails ends it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import AgentSettings
from app.platform import RunStatus, start_skill
from app.platform.agents.models import AgentRun
from app.platform.agents.policy import Blocker
from app.platform.agents.tools import ToolBox
from app.platform.modules.errors import ModuleDisabledError
from tests.helpers import (
    Account,
    FrozenClock,
    actor_of,
    build_app,
    build_settings,
    register,
    running_client,
)
from tests.platform.agents.conftest import LLM_URL, PRIMARY, calls, give_consent, reply

OPEN = ("modules_open", {"key": "journal"})


@asynccontextmanager
async def serving(
    mongo_uri: str, clock: FrozenClock, **agent: Any
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    settings = AgentSettings(_env_file=None, **({"providers": (PRIMARY,)} | agent))
    app = build_app(build_settings(mongo_uri, agent=settings), clock)
    async with running_client(app) as client:
        yield app, client


async def poll(
    client: AsyncClient, account: Account, run_id: str, until: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """What the page does: read the run until it shows what is awaited."""
    for _ in range(500):
        run: dict[str, Any] = (
            await client.get(f"/v1/agents/runs/{run_id}", headers=account.headers)
        ).json()
        if until(run):
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"the run never got there: {run}")


def finished(run: dict[str, Any]) -> bool:
    return bool(run["status"] != "running")


async def until(ready: Callable[[], bool]) -> None:
    for _ in range(500):
        if ready():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it never happened")


async def never(_request: httpx.Request) -> httpx.Response:
    await asyncio.Event().wait()
    raise AssertionError  # never answers


async def quota_left(client: AsyncClient, account: Account) -> set[int]:
    agents = (await client.get("/v1/agents", headers=account.headers)).json()["items"]
    return {agent["quota_remaining"] for agent in agents}


@respx.mock
async def test_a_background_run_answers_at_once_and_its_page_follows_the_stages(
    mongo_uri: str, clock: FrozenClock
) -> None:
    answers = [calls(("modules_list", {})), calls(OPEN), reply("Opening Journal.")]
    asked: list[httpx.Request] = []
    go_on = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if len(asked) == 2:  # the run waits here while its page polls
            await go_on.wait()
        return answers[len(asked) - 1]

    respx.post(LLM_URL).mock(side_effect=provider)
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        await give_consent(client, account)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "open my journal", locale="en"
        )
        run_id = str(started.id)
        working = await poll(client, account, run_id, lambda run: "checking" in run["stages"])
        go_on.set()
        done = await poll(client, account, run_id, finished)

    assert (started.status, started.stages) == (RunStatus.RUNNING, ["understanding"])
    assert working["status"] == "running"
    assert working["stages"] == ["understanding", "reading", "checking"]
    assert (working["client_actions"], working["message"]) == ([], None)
    assert (done["status"], done["mode"], done["message"]) == (
        "completed",
        "agent",
        "Opening Journal.",
    )
    assert done["stages"] == [
        "understanding",
        "reading",
        "checking",
        "drafting",
        "checking",
        "done",
    ]
    assert done["client_actions"] == [{"tool": "modules_open", "arguments": {"key": "journal"}}]


@respx.mock
async def test_a_run_past_its_time_ends_as_incomplete_with_reason_timeout(
    mongo_uri: str, clock: FrozenClock
) -> None:
    respx.post(LLM_URL).mock(side_effect=never)
    async with serving(mongo_uri, clock, run_timeout_seconds=0.2) as (app, client):
        account = await register(client)
        await give_consent(client, account)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "open my journal", locale="en"
        )
        done = await poll(client, account, str(started.id), finished)
        record = await AgentRun.get(started.id)

    assert (done["status"], done["reason"], done["stages"][-1]) == ("incomplete", "timeout", "done")
    assert record is not None
    assert record.status is RunStatus.INCOMPLETE  # stored so, not only read so
    assert record.reason is Blocker.TIMEOUT


@respx.mock
async def test_a_run_a_restart_cut_short_reads_as_timed_out(
    mongo_uri: str, clock: FrozenClock
) -> None:
    respx.post(LLM_URL).mock(side_effect=never)
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        await give_consent(client, account)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "open my journal", locale="en"
        )
    # The server stopped mid-run; a new one starts on the same database.
    async with serving(mongo_uri, clock) as (_app, client):
        url = f"/v1/agents/runs/{started.id}"
        before = (await client.get(url, headers=account.headers)).json()
        clock.advance(seconds=91)
        after = (await client.get(url, headers=account.headers)).json()

    assert (before["status"], before["reason"]) == ("running", None)
    assert (after["status"], after["reason"]) == ("incomplete", "timeout")


async def test_a_background_run_follows_the_same_switches(
    mongo_uri: str, clock: FrozenClock
) -> None:
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)  # no consent: the static script answers
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "pin habits", locale="en"
        )
        done = await poll(client, account, str(started.id), finished)

    assert (done["status"], done["mode"], done["reason"]) == ("completed", "static", "consent")
    assert done["stages"] == ["understanding", "done"]
    assert done["message_code"] == "manage-modules.ready.pin"


async def test_a_refusal_answers_at_once(mongo_uri: str, clock: FrozenClock) -> None:
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        await client.patch(
            "/v1/modules/expenses/preferences", headers=account.headers, json={"enabled": False}
        )
        with pytest.raises(ModuleDisabledError):
            await start_skill(await actor_of(app, account), "track-expenses", "tea 20", locale="en")
        runs = await AgentRun.find({"user_id": (await actor_of(app, account)).user.id}).count()

    assert runs == 0  # nothing was started


async def test_a_fault_ends_the_run_instead_of_leaving_it_running(
    mongo_uri: str, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the database went away")

    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        monkeypatch.setattr(app.state.agents, "blocker", broken)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "pin habits", locale="en"
        )
        done = await poll(client, account, str(started.id), finished)

    assert (done["status"], done["reason"], done["stages"]) == (
        "incomplete",
        None,
        ["understanding", "done"],
    )


@respx.mock
async def test_a_tool_that_fails_ends_the_run_and_its_words_go_nowhere(
    mongo_uri: str,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Strands would send the failure's words to the provider and go on as if it had answered."""
    words = "cluster0.example.net:27017: timed out"

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(words)

    # The same answer asks twice: once the first has failed, the second is not run.
    route = respx.post(LLM_URL).mock(
        side_effect=[calls(("modules_list", {}), ("modules_list", {})), reply("never asked")]
    )
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        await give_consent(client, account)
        monkeypatch.setattr(ToolBox, "read", broken)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "list my modules", locale="en"
        )
        done = await poll(client, account, str(started.id), finished)
        left = await quota_left(client, account)
        record = await AgentRun.get(started.id)
    output = capsys.readouterr()

    assert (done["status"], done["reason"], done["message"]) == ("incomplete", None, None)
    assert done["stages"] == ["understanding", "reading", "done"]
    assert route.call_count == 1  # never asked again, so never told
    assert record is not None
    assert [(step.tool, step.ok, step.error) for step in record.steps] == [
        ("modules_list", False, "tool_failed"),
        ("modules_list", False, "tool_skipped"),
    ]
    assert left == {29}  # one call of the day's 30
    assert words not in output.out + output.err


@respx.mock
async def test_cancel_stops_my_run_and_keeps_what_it_made(
    mongo_uri: str, clock: FrozenClock
) -> None:
    asked: list[httpx.Request] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return calls(OPEN) if len(asked) == 1 else await never(request)

    respx.post(LLM_URL).mock(side_effect=provider)
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        stranger = await register(client)
        await give_consent(client, account)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "open my journal", locale="en"
        )
        url = f"/v1/agents/runs/{started.id}/cancel"
        await until(lambda: len(asked) == 2)  # its second model call is under way
        theirs = await client.post(url, headers=stranger.headers)
        cancelled = await client.post(url, headers=account.headers)
        again = await client.post(url, headers=account.headers)
        await asyncio.sleep(0.05)
        left = await quota_left(client, account)
        record = await AgentRun.get(started.id)

    assert (theirs.status_code, theirs.json()["code"]) == (404, "NOT_FOUND")
    assert cancelled.status_code == 200, cancelled.text
    run = cancelled.json()
    assert (run["status"], run["reason"], run["mode"]) == ("incomplete", None, "agent")
    assert run["client_actions"] == [{"tool": "modules_open", "arguments": {"key": "journal"}}]
    assert run["stages"] == ["understanding", "drafting", "checking", "done"]
    assert (again.status_code, again.json()) == (200, run)  # cancelled already: as it is
    assert len(asked) == 2  # no model call after it
    assert left == {28}
    assert record is not None
    assert record.cancelled


async def test_a_finished_run_is_not_cancelled(mongo_uri: str, clock: FrozenClock) -> None:
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)  # no consent: the static script answers at once
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "pin habits", locale="en"
        )
        done = await poll(client, account, str(started.id), finished)
        late = await client.post(f"/v1/agents/runs/{started.id}/cancel", headers=account.headers)
        after = await poll(client, account, str(started.id), finished)

    assert (late.status_code, late.json()["code"]) == (409, "AGENT_RUN_FINISHED")
    assert late.json()["details"] == {"status": "completed"}
    assert after == done


@respx.mock
async def test_cancel_ends_a_run_a_restart_left_running(mongo_uri: str, clock: FrozenClock) -> None:
    respx.post(LLM_URL).mock(side_effect=never)
    async with serving(mongo_uri, clock) as (app, client):
        account = await register(client)
        await give_consent(client, account)
        started = await start_skill(
            await actor_of(app, account), "manage-modules", "open my journal", locale="en"
        )
    # The server stopped mid-run; a new one starts on the same database.
    async with serving(mongo_uri, clock) as (_app, client):
        cancelled = await client.post(
            f"/v1/agents/runs/{started.id}/cancel", headers=account.headers
        )

    assert cancelled.status_code == 200, cancelled.text
    run = cancelled.json()
    assert (run["status"], run["reason"], run["stages"][-1]) == ("incomplete", None, "done")
