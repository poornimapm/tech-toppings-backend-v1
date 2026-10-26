"""Pushing a reminder is a job of its own: the inbox never waits on a push service, every
browser is pushed at once within PUSH_TIMEOUT_SECONDS (PUSH_MAX_PARALLEL pushes at a time), a
slow browser costs only its own try, and a push not tried on every browser goes again, up to
PUSH_MAX_TRIES tries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from pymongo.errors import AutoReconnect

from app.platform.context import ActorContext
from app.platform.jobs.runner import catch_up
from app.platform.push.models import PushSubscription
from app.platform.push.service import Push
from app.platform.reminders.models import Reminder
from app.platform.reminders.service import Reminders, round_tag
from tests.helpers import (
    FrozenClock,
    actor_of,
    build_app,
    build_settings,
    json_log_lines,
    register,
    running_client,
)
from tests.platform.push.conftest import FCM, Browser, push_settings, reached, subscribe

TIMEOUT, DEVICE = 1.0, 0.2  # PUSH_TIMEOUT_SECONDS, PUSH_DEVICE_TIMEOUT_SECONDS
TRIES = 2  # PUSH_MAX_TRIES
# One push under way at a time (PUSH_MAX_PARALLEL=1): a round has time for a few, each taking
# ANSWER seconds, a little less than a browser's deadline.
QUEUED_TIMEOUT, QUEUED_DEVICE, ANSWER = 1.5, 0.5, 0.35
# Two pushes under way at a time: a round would not have time for every push to a hanging
# browser to use up its deadline (8 of them, two at a time, is 0.8 s).
CROWDED_TIMEOUT, CROWDED_DEVICE, CROWDED_MESSAGES = 0.6, 0.2, 8


@pytest.fixture
def app(mongo_uri: str, clock: FrozenClock) -> FastAPI:
    push = push_settings(timeout_seconds=TIMEOUT, device_timeout_seconds=DEVICE, max_tries=TRIES)
    return build_app(build_settings(mongo_uri, push=push), clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with running_client(app) as http:
        yield http


@pytest.fixture
async def queued(mongo_uri: str, clock: FrozenClock) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    push = push_settings(
        timeout_seconds=QUEUED_TIMEOUT, device_timeout_seconds=QUEUED_DEVICE, max_parallel=1
    )
    app = build_app(build_settings(mongo_uri, push=push), clock)
    async with running_client(app) as http:
        yield app, http


@pytest.fixture
async def crowded(mongo_uri: str, clock: FrozenClock) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    push = push_settings(
        timeout_seconds=CROWDED_TIMEOUT, device_timeout_seconds=CROWDED_DEVICE, max_parallel=2
    )
    app = build_app(build_settings(mongo_uri, push=push), clock)
    async with running_client(app) as http:
        yield app, http


async def due_reminder(
    app: FastAPI,
    client: AsyncClient,
    clock: FrozenClock,
    *browsers: Browser,
    refs: tuple[str, ...] = ("n-1",),
) -> ActorContext:
    """An account whose browsers receive pushes, with reminders that came due yesterday."""
    account = await register(client)
    for browser in browsers:
        assert (await subscribe(client, account, browser)).status_code == 200
    ctx = await actor_of(app, account)
    for ref in refs:
        await Reminders(ctx).schedule(
            "notes.due", module="notes", ref_id=ref, due_on=ctx.today(), title="Pay rent"
        )
    clock.advance(days=1)
    return ctx


async def mine(ctx: ActorContext) -> list[Reminder]:
    return await Reminder.find({"user_id": ctx.user.id}).sort("ref_id").to_list()


def yesterday(ctx: ActorContext) -> str:
    return (ctx.today() - timedelta(days=1)).isoformat()


@contextmanager
def hanging_push_service(*fast: Browser) -> Iterator[tuple[respx.Route, list[str]]]:
    """A push service that never answers, but at once for the ``fast`` browsers. Yields its
    route (what it took) and where it was asked to push: a call cut short is never recorded."""
    answers, asked = {browser.endpoint for browser in fast}, []

    async def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if str(request.url) not in answers:
            await asyncio.sleep(30)
        return httpx.Response(201)

    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(url__startswith=FCM).mock(side_effect=answer), asked


@contextmanager
def slow_push_service(browser: Browser) -> Iterator[list[dict[str, Any]]]:
    """A push service that takes ANSWER seconds to take each message for ``browser``. Yields
    what it was asked to push, as each push began."""
    asked: list[dict[str, Any]] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        asked.append(browser.read(request.content))
        await asyncio.sleep(ANSWER)
        return httpx.Response(201)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith=FCM).mock(side_effect=answer)
        yield asked


async def noted(ctx: ActorContext) -> dict[str, Any]:
    """When each of the user's browsers last took a push."""
    browsers = await PushSubscription.find({"user_id": ctx.user.id}).to_list()
    return {item.endpoint: item.last_sent_at for item in browsers}


def stall_after_posting(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """Pushes to a browser whose endpoint, or of a message whose JSON, holds ``where`` stall once
    the push service took them, as a stalled database would. No browser's deadline covers that,
    so PUSH_TIMEOUT_SECONDS cuts the round short."""
    post = Push._post

    async def stalled(self: Push, browser: PushSubscription, payload: bytes) -> int:
        took = await post(self, browser, payload)
        if where in browser.endpoint or where in payload.decode():
            await asyncio.sleep(30)
        return took

    monkeypatch.setattr(Push, "_post", stalled)


async def test_a_due_reminder_is_told_then_pushed_to_each_browser_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock, push_service: respx.Route
) -> None:
    phone, laptop = Browser("reminded-phone"), Browser("reminded-laptop")
    ctx = await due_reminder(app, client, clock, phone, laptop)

    assert await Reminders(ctx).deliver_due() == 1
    assert push_service.calls.call_count == 0  # telling never waits on a push service
    assert await Reminders(ctx).push_due() == 1
    assert await Reminders(ctx).push_due() == 0  # pushed: never again

    [message] = reached(push_service, phone)
    assert reached(push_service, laptop) == [message]
    params = message["params"]
    assert (params["module"], params["title"], params["due_on"]) == (
        "notes",
        "Pay rent",
        yesterday(ctx),
    )
    assert message["tag"] == f"reminder:{params['reminder_id']}:0"


async def test_a_hanging_browser_costs_its_own_try_and_the_round_completes(
    app: FastAPI, client: AsyncClient, clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    hanging, phone = Browser("hanging-laptop"), Browser("fast-phone")
    ctx = await due_reminder(app, client, clock, hanging, phone)
    capsys.readouterr()

    with hanging_push_service(phone) as (route, asked):
        sent = await Push(ctx).send({"type": "push.test", "params": {}})

    assert (sent.delivered, sent.complete) == (1, True)
    assert sorted(asked) == sorted([hanging.endpoint, phone.endpoint])
    assert reached(route, phone) == [{"type": "push.test", "params": {}}]
    out = capsys.readouterr().out
    failed = [event for event in json_log_lines(out) if event["event"] == "push_failed"]
    assert [(event["host"], event["error_type"]) for event in failed] == [
        ("fcm.googleapis.com", "TimeoutError")
    ]
    assert "hanging-laptop" not in out  # a log names the host, never the endpoint's path
    assert await noted(ctx) == {hanging.endpoint: None, phone.endpoint: clock.now()}


async def test_a_push_waiting_its_turn_keeps_the_browser_its_own_time(
    queued: tuple[FastAPI, AsyncClient], clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    app, client = queued
    phone = Browser("patient-phone")
    ctx = await due_reminder(app, client, clock, phone, refs=())
    capsys.readouterr()
    first, second = ({"type": "push.test", "params": {"n": n}} for n in (1, 2))

    with slow_push_service(phone) as asked:
        sent = await Push(ctx).send(first, second)

    assert asked == [first, second]  # one after the other, earliest first
    assert (sent.delivered, sent.done) == (2, (True, True))  # each within its own deadline
    events = [event["event"] for event in json_log_lines(capsys.readouterr().out)]
    assert "push_failed" not in events
    assert await noted(ctx) == {phone.endpoint: clock.now()}


async def test_a_hanging_browser_is_tried_once_a_round_and_holds_no_one_else_up(
    crowded: tuple[FastAPI, AsyncClient], clock: FrozenClock
) -> None:
    app, client = crowded
    hanging, phone = Browser("crowding-laptop"), Browser("crowded-phone")
    ctx = await due_reminder(app, client, clock, hanging, phone, refs=())
    messages = [{"type": "push.test", "params": {"n": n}} for n in range(CROWDED_MESSAGES)]

    with hanging_push_service(phone) as (route, asked):
        sent = await Push(ctx).send(*messages)

    assert (sent.delivered, sent.complete) == (CROWDED_MESSAGES, True)
    assert reached(route, phone) == messages
    assert asked.count(hanging.endpoint) <= 2  # those under way when it first failed, no more


async def test_a_note_that_fails_never_undoes_a_round_the_push_services_took(
    app: FastAPI,
    client: AsyncClient,
    clock: FrozenClock,
    push_service: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    phone = Browser("unnoted-phone")
    ctx = await due_reminder(app, client, clock, phone)
    await Reminders(ctx).deliver_due()
    real = PushSubscription.get_pymongo_collection()

    class Unwritable:
        """The browsers' collection while the database refuses writes to it."""

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        async def update_many(self, *_: Any, **__: Any) -> None:
            raise AutoReconnect("primary stepped down")

    monkeypatch.setattr(PushSubscription, "get_pymongo_collection", Unwritable)
    capsys.readouterr()

    assert await Reminders(ctx).push_due() == 1
    monkeypatch.undo()

    assert len(reached(push_service, phone)) == 1
    [item] = await mine(ctx)
    assert item.push_at is None  # pushed: never again
    events = [event["event"] for event in json_log_lines(capsys.readouterr().out)]
    assert "push_note_failed" in events


async def test_pushes_still_waiting_when_the_round_ends_stay_owed(
    queued: tuple[FastAPI, AsyncClient], clock: FrozenClock
) -> None:
    app, client = queued
    phone = Browser("queued-phone")
    refs = tuple(f"n-{n}" for n in range(6))  # more than a round has time for, one at a time
    ctx = await due_reminder(app, client, clock, phone, refs=refs)
    assert await Reminders(ctx).deliver_due() == len(refs)

    with slow_push_service(phone) as asked:
        pushed = await Reminders(ctx).push_due()

    begun = {message["tag"] for message in asked}
    items = await mine(ctx)
    waiting = [item for item in items if round_tag(item) not in begun]
    assert pushed >= 1
    assert waiting  # never tried: the round ended first
    assert all(item.push_at is not None and item.push_tries == 1 for item in waiting)
    assert pushed == sum(item.push_at is None for item in items)

    clock.advance(seconds=ctx.settings.jobs.lease_seconds)  # their claim passes: they go again
    with hanging_push_service(phone):
        assert await Reminders(ctx).push_due() == len(refs) - pushed
    assert [item.push_at for item in await mine(ctx)] == [None] * len(refs)


async def test_past_a_hanging_browser_a_reminder_is_pushed_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    hanging, phone = Browser("hung-laptop"), Browser("quick-phone")
    ctx = await due_reminder(app, client, clock, hanging, phone)
    await Reminders(ctx).deliver_due()

    with hanging_push_service(phone) as (route, asked):
        assert await Reminders(ctx).push_due() == 1  # tried on every browser: done
        clock.advance(seconds=ctx.settings.jobs.lease_seconds)
        assert await Reminders(ctx).push_due() == 0

    assert len(reached(route, phone)) == 1
    assert asked.count(hanging.endpoint) == 1
    [item] = await mine(ctx)
    assert (item.push_at, item.push_tries) == (None, 1)


async def test_a_round_cut_short_keeps_the_messages_it_finished(
    app: FastAPI,
    client: AsyncClient,
    clock: FrozenClock,
    push_service: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Browser("progress-phone")
    ctx = await due_reminder(app, client, clock, phone, refs=("n-1", "n-2"))
    assert await Reminders(ctx).deliver_due() == 2
    first, second = await mine(ctx)
    stall_after_posting(monkeypatch, round_tag(second))

    assert await Reminders(ctx).push_due() == 1  # the first; the round was cut short
    monkeypatch.undo()
    assert await Reminders(ctx).push_due() == 0  # the second is claimed still
    clock.advance(seconds=ctx.settings.jobs.lease_seconds)
    assert await Reminders(ctx).push_due() == 1  # its claim passed: it goes again

    tags = [message["tag"] for message in reached(push_service, phone)]
    assert tags.count(round_tag(first)) == 1
    assert tags.count(round_tag(second)) == 2  # at least once: the same tag replaces it
    assert [item.push_at for item in await mine(ctx)] == [None, None]


async def test_a_push_is_tried_at_most_push_max_tries_times(
    app: FastAPI,
    client: AsyncClient,
    clock: FrozenClock,
    push_service: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stalled, phone = Browser("stalled-laptop"), Browser("capped-phone")
    ctx = await due_reminder(app, client, clock, stalled, phone)
    await Reminders(ctx).deliver_due()
    stall_after_posting(monkeypatch, stalled.endpoint)

    for _ in range(TRIES + 2):  # every round cut short; then its claim passes
        assert await Reminders(ctx).push_due() == 0
        clock.advance(seconds=ctx.settings.jobs.lease_seconds)

    assert len(reached(push_service, phone)) == TRIES  # never the same tag again past the cap
    [item] = await mine(ctx)
    assert (item.push_at, item.push_tries) == (None, TRIES)  # owed no more

    monkeypatch.undo()
    await Reminders(ctx).snooze(str(item.id), 1)
    clock.advance(days=2)
    assert await Reminders(ctx).deliver_due() == 1
    assert await Reminders(ctx).push_due() == 1  # the next round has tries of its own


async def test_two_runs_at_once_push_a_reminder_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock, push_service: respx.Route
) -> None:
    phone = Browser("raced-phone")
    ctx = await due_reminder(app, client, clock, phone)
    await Reminders(ctx).deliver_due()

    pushed = await asyncio.gather(Reminders(ctx).push_due(), Reminders(ctx).push_due())

    assert sorted(pushed) == [0, 1]
    assert len(reached(push_service, phone)) == 1


async def test_a_hanging_push_service_fails_no_job_and_never_holds_up_the_inbox(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    ctx = await due_reminder(app, client, clock, Browser("hanging"))
    within = ctx.settings.jobs.lazy_timeout_seconds  # the bell's poll

    with hanging_push_service() as (_, asked):
        told = await catch_up(ctx, within=within, only="reminders_deliver")
        pushed = await catch_up(ctx, within=within, only="reminders_push")

    assert len(asked) == 1
    assert (told.ran, told.failed, told.done) == (1, 0, 1)
    assert (pushed.ran, pushed.failed, pushed.done) == (1, 0, 1)  # tried: the browser's try failed
    [item] = await mine(ctx)
    assert item.sent_at is not None
    assert item.push_at is None  # tried on every browser: owed no more


async def test_no_push_is_owed_without_a_browser_to_push_to(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    ctx = await due_reminder(app, client, clock)

    assert await Reminders(ctx).deliver_due() == 1
    [item] = await mine(ctx)
    assert item.push_at is None
    assert await Reminders(ctx).push_due() == 0
