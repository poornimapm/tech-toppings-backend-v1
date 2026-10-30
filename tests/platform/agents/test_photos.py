"""Photo runs (a receipt): the photo goes only to a provider that reads images, is never kept
anywhere, and without AI the scan falls back to text read on the device."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from beanie import PydanticObjectId
from httpx import AsyncClient, Response
from pymongo import AsyncMongoClient

from app.core.config import AgentSettings, ProviderSettings
from app.platform.agents.models import AgentRun
from tests.helpers import Account, FrozenClock, build_app, build_settings, register, running_client
from tests.platform.agents.conftest import (
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
MARKER = b"TT-RECEIPT-PHOTO-MARKER"
PHOTO = b"\xff\xd8\xff\xe0" + MARKER * 200  # a JPEG by its first bytes
ENCODED = base64.b64encode(PHOTO).decode()
SCAN = "/v1/agents/assistant/photo-runs?skill=scan-receipt"
DRAFT = {
    "title": "Tomato",
    "amount": 80,
    "category_id": "groceries",
    "merchant": "Sri Murugan Stores",
    "date": "2026-10-12",
}
RECEIPT = """SRI MURUGAN STORES
Date: 12/10/2026
Tomato 1kg      2  40.00   80.00
Toor Dal 1 Kg   1 145.00  145.00
Grand Total             225.00
Cash                    500.00"""


async def scan(
    client: AsyncClient, account: Account, photo: bytes = PHOTO, kind: str = "image/jpeg"
) -> Response:
    return await client.post(SCAN, content=photo, headers=account.headers | {"Content-Type": kind})


def values(found: Any) -> Iterator[str | bytes]:
    """Every text and binary value in a stored document, at any depth."""
    if isinstance(found, dict):
        for item in found.values():
            yield from values(item)
    elif isinstance(found, list):
        for item in found:
            yield from values(item)
    elif isinstance(found, str | bytes):
        yield found


async def everything_stored(uri: str) -> list[str | bytes]:
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(uri)
    try:
        database = client.get_default_database()
        found: list[str | bytes] = []
        for name in await database.list_collection_names():
            async for document in database[name].find():
                found += values(document)
        return found
    finally:
        await client.close()


def holds_the_photo(value: str | bytes) -> bool:
    if isinstance(value, bytes):
        return MARKER in value
    return MARKER.decode() in value or ENCODED[:40] in value


@respx.mock
async def test_a_photo_goes_only_to_a_provider_that_reads_images_and_is_never_kept(
    own_mongo_uri: str, clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    text_only = respx.post(LLM_URL).mock(return_value=reply("unused"))
    vision = respx.post(VISION_URL).mock(
        side_effect=[
            calls(("expenses_categories", {}), ("expenses_habits", {})),
            calls(("expenses_draft", {"drafts": [DRAFT]})),
            reply("I prepared 1 draft for 80."),
        ]
    )
    agent = AgentSettings(_env_file=None, providers=(PRIMARY, VISION), fallback=False)
    settings = build_settings(own_mongo_uri, agent=agent)
    async with running_client(build_app(settings, clock)) as client:
        account = await register(client)
        await give_consent(client, account)
        response = await scan(client, account)
        run = response.json()
        again = await client.get(f"/v1/agents/runs/{run['id']}", headers=account.headers)
        stored = await everything_stored(own_mongo_uri)

    assert response.status_code == 201, response.text
    assert (run["mode"], run["skill"], run["status"]) == ("agent", "scan-receipt", "completed")
    [action] = run["client_actions"]
    assert action["tool"] == "expenses_draft"
    assert action["arguments"]["drafts"][0]["title"] == "Tomato"
    assert again.json()["client_actions"] == run["client_actions"]
    assert text_only.call_count == 0
    # Every step sends the photo again, as the user's message: the image part alone.
    assert vision.call_count == 3
    for index in range(vision.call_count):
        [user] = [m for m in sent(vision, index)["messages"] if m["role"] == "user"]
        assert user["content"] == [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{ENCODED}"}}
        ]
    # Nothing kept it: not the run, not any other record, not the logs.
    assert stored, "the run and the account are stored"
    assert not [value for value in stored if holds_the_photo(value)]
    output = capsys.readouterr()
    assert not holds_the_photo(output.out + output.err)


@respx.mock
async def test_the_run_records_how_many_photos_it_read_and_no_words(
    agent_client: AgentClient,
) -> None:
    respx.post(VISION_URL).mock(return_value=reply("That isn't a receipt."))
    async with agent_client(providers=[VISION]) as client:
        account = await register(client)
        await give_consent(client, account)
        run = (await scan(client, account)).json()
        stored = await AgentRun.get(PydanticObjectId(run["id"]))

    assert stored is not None
    assert (stored.photos, stored.input) == (1, "")
    assert run["message"] == "That isn't a receipt."
    assert run["client_actions"] == []


@respx.mock
@pytest.mark.parametrize(
    ("consent", "providers", "reason"),
    [(True, [PRIMARY], "no_provider"), (False, [VISION], "consent")],
)
async def test_without_ai_for_photos_the_app_is_told_to_read_it_on_the_device(
    agent_client: AgentClient, consent: bool, providers: list[ProviderSettings], reason: str
) -> None:
    text_only = respx.post(LLM_URL).mock(return_value=reply("unused"))
    vision = respx.post(VISION_URL).mock(return_value=reply("unused"))
    async with agent_client(providers=providers) as client:
        account = await register(client)
        if consent:
            await give_consent(client, account)
        response = await scan(client, account)

    run = response.json()
    assert response.status_code == 201, response.text
    assert (run["mode"], run["reason"]) == ("static", reason)
    assert run["message_code"] == "scan-receipt.on_device"
    assert run["client_actions"] == []
    assert text_only.call_count == vision.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    ("answers", "agent", "reason"),
    [
        # The provider refuses the first call, its error echoing what it was sent.
        (
            [Response(400, json={"error": {"message": f"bad image {ENCODED[:400]}"}})],
            {},
            "providers_failed",
        ),
        # It reads the categories, then is out of breath (429) before any draft.
        ([calls(("expenses_categories", {})), Response(429)], {}, "providers_failed"),
        # The user has one AI call left today: the scan needs more.
        ([calls(("expenses_categories", {}))], {"user_daily_calls": 1}, "quota"),
    ],
)
async def test_when_ai_stops_before_any_draft_the_photo_is_read_on_the_device_and_never_kept(
    own_mongo_uri: str,
    clock: FrozenClock,
    capsys: pytest.CaptureFixture[str],
    answers: list[Response],
    agent: dict[str, Any],
    reason: str,
) -> None:
    respx.post(VISION_URL).mock(side_effect=answers)
    settings = AgentSettings(_env_file=None, providers=(VISION,), fallback=True, **agent)
    app = build_app(build_settings(own_mongo_uri, agent=settings), clock)
    async with running_client(app) as client:
        account = await register(client)
        await give_consent(client, account)
        response = await scan(client, account)
        stored = await everything_stored(own_mongo_uri)

    run = response.json()
    assert response.status_code == 201, response.text
    assert (run["mode"], run["reason"], run["status"]) == ("static", reason, "completed")
    assert run["message_code"] == "scan-receipt.on_device"
    assert not [value for value in stored if holds_the_photo(value)]
    output = capsys.readouterr()
    assert not holds_the_photo(output.out + output.err)


@respx.mock
async def test_a_scan_gets_room_to_draft_a_long_receipt(agent_client: AgentClient) -> None:
    vision = respx.post(VISION_URL).mock(return_value=reply("No receipt here."))
    text = respx.post(LLM_URL).mock(return_value=reply("Hello."))
    async with agent_client(providers=[PRIMARY, VISION]) as client:
        account = await register(client)
        await give_consent(client, account)
        await scan(client, account)
        await ask(client, account, "hello")

    assert sent(vision)["max_tokens"] == 6000  # the skill's max-output-tokens
    assert sent(text)["max_tokens"] == 800  # the provider's own, for everything else


@respx.mock
async def test_a_resting_vision_provider_costs_no_ai_calls(agent_client: AgentClient) -> None:
    vision = respx.post(VISION_URL).mock(side_effect=[Response(429)])
    async with agent_client(providers=[VISION], user_daily_calls=5) as client:
        account = await register(client)
        await give_consent(client, account)
        first = (await scan(client, account)).json()
        second = (await scan(client, account)).json()
        agents = (await client.get("/v1/agents", headers=account.headers)).json()["items"]

    assert vision.call_count == 1  # resting after its 429: not asked again
    assert (first["reason"], second["reason"]) == ("providers_failed", "providers_failed")
    assert agents[0]["quota_remaining"] == 4  # only the call that was made


@respx.mock
async def test_a_switched_off_module_takes_no_photo(agent_client: AgentClient) -> None:
    vision = respx.post(VISION_URL).mock(return_value=reply("unused"))
    async with agent_client(providers=[VISION]) as client:
        account = await register(client)
        await give_consent(client, account)
        off = await client.patch(
            "/v1/modules/expenses/preferences", headers=account.headers, json={"enabled": False}
        )
        response = await scan(client, account)

    assert off.status_code == 200, off.text
    assert (response.status_code, response.json()["code"]) == (403, "MODULE_DISABLED")
    assert vision.call_count == 0


async def test_only_a_skill_that_reads_photos_takes_one(agent_client: AgentClient) -> None:
    async with agent_client() as client:
        account = await register(client)
        response = await client.post(
            "/v1/agents/assistant/photo-runs?skill=track-expenses",
            content=PHOTO,
            headers=account.headers | {"Content-Type": "image/jpeg"},
        )

    assert (response.status_code, response.json()["code"]) == (422, "AGENT_SKILL_TAKES_NO_PHOTOS")


@pytest.mark.parametrize(
    ("agent", "consent", "photo_mode"),
    [
        ({"providers": [VISION]}, True, "agent"),
        ({"providers": [PRIMARY]}, True, "static"),  # no provider reads images
        ({"providers": [VISION], "skill_modes": "scan-receipt=static"}, True, "static"),
        ({"providers": [VISION]}, False, "static"),  # AI off for the account
        ({"providers": [PRIMARY], "fallback": False}, True, None),  # nothing would read it
    ],
)
async def test_the_assistant_says_where_a_scans_photo_would_be_read(
    agent_client: AgentClient, agent: dict[str, Any], consent: bool, photo_mode: str | None
) -> None:
    async with agent_client(**agent) as client:
        account = await register(client)
        if consent:
            await give_consent(client, account)
        [assistant] = (await client.get("/v1/agents", headers=account.headers)).json()["items"]

    skills = {skill["name"]: skill for skill in assistant["skills"]}
    assert skills["scan-receipt"]["photo_mode"] == photo_mode
    assert skills["track-expenses"]["photo_mode"] is None  # it takes no photos


def test_a_photo_must_fit_in_a_request(mongo_uri: str) -> None:
    with pytest.raises(ValueError, match="AGENT_MAX_PHOTO_BYTES must be at most"):
        build_settings(mongo_uri, agent=AgentSettings(_env_file=None, max_photo_bytes=5_000_000))


@pytest.mark.parametrize(
    ("photo", "kind"),
    [
        (b"%PDF-1.7 not a photo", "image/jpeg"),  # the bytes decide, not the header
        (b"", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + MARKER, "image/png"),  # a PNG, which this server doesn't take
    ],
)
async def test_refuses_what_isnt_a_photo_it_takes(
    agent_client: AgentClient, photo: bytes, kind: str
) -> None:
    async with agent_client(photo_types="image/jpeg,image/webp") as client:
        account = await register(client)
        response = await scan(client, account, photo, kind)

    assert response.status_code == 415
    assert response.json()["code"] == "AGENT_PHOTO_TYPE"
    assert response.json()["details"] == {"types": ["image/jpeg", "image/webp"]}


async def test_refuses_a_photo_over_the_limit(agent_client: AgentClient) -> None:
    async with agent_client(max_photo_bytes=10_000) as client:
        account = await register(client)
        response = await scan(client, account, PHOTO + b"0" * 10_000)

    assert response.status_code == 413
    assert response.json()["code"] == "AGENT_PHOTO_TOO_LARGE"


async def test_a_photo_needs_a_skill_and_a_known_agent(agent_client: AgentClient) -> None:
    async with agent_client() as client:
        account = await register(client)
        no_skill = await client.post(
            "/v1/agents/assistant/photo-runs", content=PHOTO, headers=account.headers
        )
        no_agent = await client.post(
            "/v1/agents/nobody/photo-runs?skill=scan-receipt",
            content=PHOTO,
            headers=account.headers,
        )

    assert no_skill.status_code == 422
    assert (no_agent.status_code, no_agent.json()["code"]) == (404, "AGENT_NOT_FOUND")


# ------------------------------------------------------------------ text read on the device


async def test_text_read_on_the_device_becomes_drafts_without_ai(
    agent_client: AgentClient,
) -> None:
    long_receipt = RECEIPT + "\n" + "Thank you, visit again!\n" * 60  # longer than typed input
    async with agent_client(max_input_chars=1000) as client:
        account = await register(client)
        run = await ask(
            client,
            account,
            long_receipt,
            skill="scan-receipt",
            locale="en",
            context={"input_type": "ocr"},
        )

    assert len(long_receipt) > 1000
    assert (run["mode"], run["reason"]) == ("static", "consent")
    assert run["message_code"] == "scan-receipt.ready"
    assert run["message_params"] == {"count": 2, "total": 225.0}
    [action] = run["client_actions"]
    assert [(d["title"], d["amount"], d["date"]) for d in action["arguments"]["drafts"]] == [
        ("Tomato 1kg", 80.0, "2026-10-12"),
        ("Toor Dal 1 Kg", 145.0, "2026-10-12"),
    ]
    assert action["arguments"]["drafts"][0]["merchant"] == "Sri Murugan Stores"


@pytest.mark.parametrize(
    ("skill", "input_type", "limit"),
    [
        ("scan-receipt", "ocr", 2000),
        ("scan-receipt", "text", 1000),
        # Only a skill that reads text from photos takes the longer limit.
        ("track-expenses", "ocr", 1000),
        (None, "ocr", 1000),
    ],
)
async def test_text_read_from_a_photo_has_its_own_limit(
    agent_client: AgentClient, skill: str | None, input_type: str, limit: int
) -> None:
    async with agent_client(max_input_chars=1000, max_ocr_chars=2000) as client:
        account = await register(client)
        response = await client.post(
            "/v1/agents/assistant/runs",
            headers=account.headers,
            json={
                "input": "x" * (limit + 1),
                "skill": skill,
                "context": {"input_type": input_type},
            },
        )

    assert response.status_code == 422
    assert response.json()["details"] == {"max": limit}


async def test_unreadable_text_says_so(agent_client: AgentClient) -> None:
    async with agent_client() as client:
        account = await register(client)
        run = await ask(client, account, "~~~ ## ~~~", skill="scan-receipt")

    assert (run["message_code"], run["client_actions"]) == ("scan-receipt.unreadable", [])


async def test_the_general_assistant_never_reads_words_as_a_receipt(
    agent_client: AgentClient,
) -> None:
    async with agent_client() as client:
        account = await register(client)
        run = await ask(client, account, "Tomato 40.00\nGrand Total 40.00")

    assert (run["skill"], run["message_code"]) == (None, "static.no_match")
