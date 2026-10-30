"""Marking a day by voice or text (the mark-attendance skill): the rules reader in English,
Tamil and Hindi, what it hands the page, and the skill through the API, by the rules and with
the AI. Nothing is ever saved by it: the owner saves the draft."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import respx
import yaml
from httpx import AsyncClient
from pydantic import ValidationError

from app.modules.labor.marks_reader import RosterWorker, load_words, read_marks
from app.modules.labor.tools import labor_attendance_draft, labor_day
from app.platform.agents.tools import TOOL_ATTRIBUTE, ToolSpec
from tests.helpers import Account, register
from tests.modules.labor.conftest import LABOR, Owner, day_of, owner_account
from tests.platform.admin.conftest import admin_account
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

REFERENCES = Path(__file__).parents[3] / "app/modules/labor/skills/mark-attendance/references"
WORDS = load_words(REFERENCES)
DRAFT_TOOL: ToolSpec = getattr(labor_attendance_draft, TOOL_ATTRIBUTE)
DAY_TOOL: ToolSpec = getattr(labor_day, TOOL_ATTRIBUTE)
ROSTER = [
    RosterWorker("w1", "Ravi Kumar", "W001"),
    RosterWorker("w2", "Meena", "W002"),
    RosterWorker("w3", "Lakshmi", "W003"),
    RosterWorker("w4", "ரவி", "W004"),
    RosterWorker("w5", "सुनीता", "W005"),
    RosterWorker("w6", "Ravi Shankar", "W006"),
]
Read = tuple[list[tuple[str, str | None, list[str]]], str | None]
CURLY = chr(0x2019)  # the apostrophe phones type


def read(text: str) -> Read:
    draft = read_marks(text, ROSTER, WORDS)
    return [(m.worker_id or m.name, m.status, m.uncertain) for m in draft.marks], draft.rest


# ---------------------------------------------------------------------- the rules reader


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Meena full day, Lakshmi half day, the rest absent",
            ([("w2", "full", []), ("w3", "half", [])], "absent"),
        ),
        # A sentence may start with its mark; numbers name workers too.
        (
            "Full day: Meena, Lakshmi. W001 half day",
            ([("w2", "full", []), ("w3", "full", []), ("w1", "half", [])], None),
        ),
        (
            "Meena and Lakshmi came, number 1 did not come",
            ([("w2", "full", []), ("w3", "full", []), ("w1", "absent", [])], None),
        ),
        ("everyone full day", ([], "full")),
        ("ரவி முழு நாள், மற்றவர்கள் வரவில்லை", ([("w4", "full", [])], "absent")),
        ("ரவி அரை நாள்", ([("w4", "half", [])], None)),
        ("सुनीता आधा दिन, बाकी सब पूरा दिन", ([("w5", "half", [])], "full")),
        ("सुनीता नहीं आई", ([("w5", "absent", [])], None)),  # not "आई" (came)
        ("meena poora din, lakshmi aadha din", ([("w2", "full", []), ("w3", "half", [])], None)),
    ],
)
def test_marks_are_read_in_english_tamil_and_hindi(text: str, expected: Read) -> None:
    assert read(text) == expected


def test_what_is_not_sure_is_handed_over_to_settle() -> None:
    # Two Ravis: either may be meant. Close to Lakshmi. Nobody called Suresh. No mark for Meena.
    assert read("Ravi full day, Laxmi half day, Suresh absent, Meena") == (
        [
            ("w1", "full", ["worker"]),
            ("w3", "half", ["worker"]),
            ("w2", None, ["status"]),
            ("suresh", "absent", ["worker"]),
        ],
        None,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "came" is a full day, unless a mark is said straight after it.
        ("Lakshmi came half day", ([("w3", "half", ["status"])], None)),
        ("Lakshmi came, half day", ([("w3", "half", ["status"])], None)),
        # Starting with a mark, a comma part with a mark of its own keeps it.
        (
            "Full day: Meena, Lakshmi, number 1 absent",
            ([("w2", "full", []), ("w3", "full", []), ("w1", "absent", [])], None),
        ),
        ("Everyone full day, Lakshmi half day", ([("w3", "half", [])], "full")),
        # हाजिरी (attendance) is no हाजिर (present).
        ("आज की हाजिरी: सुनीता आधा दिन, बाकी सब पूरा दिन", ([("w5", "half", [])], "full")),
        # A phone's curly apostrophe, and the common spellings of "didn't come".
        (
            f"Lakshmi didn{CURLY}t come, Meena full day",
            ([("w3", "absent", []), ("w2", "full", [])], None),
        ),
        ("Lakshmi didn't came", ([("w3", "absent", [])], None)),
        ("सुनीता नही आई", ([("w5", "absent", [])], None)),
        # A long run of unknown words is talk, not a name.
        ("Meena full day, " + "x" * 90 + " absent", ([("w2", "full", [])], None)),
    ],
)
def test_common_ways_of_saying_it(text: str, expected: Read) -> None:
    assert read(text) == expected


def test_two_workers_with_one_name_or_number_are_left_to_choose() -> None:
    roster = [
        RosterWorker("a", "Ravi", "W001"),
        RosterWorker("b", "Ravi", "W002"),
        RosterWorker("c", "Meena", "M3"),
        RosterWorker("d", "Kumar", "F3"),
        RosterWorker("e", "K. Murugan", "W010"),
        RosterWorker("f", "S. Murugan", "W011"),
    ]

    draft = read_marks("Ravi absent, number 3 full day. K. Murugan half day", roster, WORDS)

    assert [(m.worker_id, m.status, m.uncertain) for m in draft.marks] == [
        ("a", "absent", ["worker"]),
        ("c", "full", ["worker"]),
        ("e", "half", []),  # an initial is part of a name, not the end of a sentence
    ]


def test_a_long_text_for_a_large_crew_is_read_quickly() -> None:
    crew = [RosterWorker(f"w{i}", f"Name{i} Surname{i}", f"W{i:03}") for i in range(500)]
    for text in ("full " + "a." * 497, "full " + "q." * 497, "full " + "Name3 " * 160):
        started = time.perf_counter()
        read_marks(text[:999], crew, WORDS)
        assert time.perf_counter() - started < 3  # was over 30 s: one pattern per name, per part


def test_every_word_is_text() -> None:
    # Bare YAML reads "on", "no" and "yes" as booleans: each word must stay a word.
    data = yaml.safe_load((REFERENCES / "attendance.yaml").read_text(encoding="utf-8"))
    lists = [data[key] for key in ("present", "rest", "everyone", "fillers", "number")]
    lists += [*data["statuses"].values(), *data["days"].values()]
    assert all(isinstance(word, str) for words in lists for word in words)


def test_a_worker_named_twice_keeps_the_last_mark() -> None:
    assert read("Meena full day. Meena half day") == ([("w2", "half", [])], None)


def test_words_without_a_mark_are_not_attendance() -> None:
    assert read("Ravi paid 500") == ([], None)
    assert read_marks("Meena", ROSTER, WORDS).empty


def test_a_day_said_is_kept() -> None:
    assert read_marks("number 3 half day yesterday", ROSTER, WORDS).days_back == 1
    assert read_marks("कल सुनीता पूरा दिन", ROSTER, WORDS).days_back == 1  # कल: yesterday


def test_what_it_hands_over_is_what_the_tool_takes() -> None:
    draft = read_marks("Meena full day, Suresh half day, the rest absent", ROSTER, WORDS)
    arguments = draft.arguments("p1", date(2026, 10, 30))

    parsed = DRAFT_TOOL.parse({"draft": arguments}).model_dump()["draft"]

    assert parsed["day"] == "2026-10-30"
    assert [(mark["worker_id"], mark["name"]) for mark in parsed["marks"]] == [
        ("w2", "Meena"),
        (None, "suresh"),
    ]
    # A day that doesn't exist is refused as the model's mistake (told back to it), not a crash.
    with pytest.raises(ValidationError):
        DRAFT_TOOL.parse({"draft": arguments | {"day": "2026-09-31"}})
    with pytest.raises(ValidationError):
        DAY_TOOL.parse({"day": "2026-09-31"})


# ---------------------------------------------------------------------- through the API


async def farm_with(client: AsyncClient, owner: Account, *names: str) -> tuple[str, list[str]]:
    farm = await client.post(f"{LABOR}/projects", headers=owner.headers, json={"name": "Farm"})
    assert farm.status_code == 201, farm.text
    ids = []
    for name in names:
        made = await client.post(
            f"{LABOR}/projects/{farm.json()['id']}/workers",
            headers=owner.headers,
            json={"name": name},
        )
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    return farm.json()["id"], ids


async def test_the_page_gets_a_draft_to_check_and_nothing_is_saved(
    client: AsyncClient, owner: Owner
) -> None:
    farm, (meena, _) = await farm_with(client, owner.account, "Meena", "Ravi")
    yesterday = (day_of(owner) - timedelta(days=1)).isoformat()
    url = f"{LABOR}/projects/{farm}/attendance/read"
    headers = owner.account.headers

    read_ = await client.post(
        url,
        headers=headers,
        params={"day": yesterday},
        json={"text": "Meena full day, the rest absent", "input_type": "speech", "locale": "en"},
    )
    nothing = await client.post(url, headers=headers, json={"text": "good morning"})
    sheet = await client.get(
        f"{LABOR}/projects/{farm}/attendance", headers=headers, params={"day": yesterday}
    )

    tomorrow = (day_of(owner) + timedelta(days=1)).isoformat()
    later = await client.post(url, headers=headers, params={"day": tomorrow}, json={"text": "x"})

    assert read_.status_code == 200, read_.text
    body = read_.json()
    # Meena, and Ravi by "the rest".
    assert (body["mode"], body["message_code"], body["message_params"]) == (
        "static",
        "mark-attendance.ready",
        {"count": 2},
    )
    assert (later.status_code, later.json()["code"]) == (422, "LABOR_DAY_IN_FUTURE")
    assert body["draft"] == {
        "project_id": farm,
        "day": yesterday,
        "marks": [{"worker_id": meena, "name": "Meena", "status": "full", "uncertain": []}],
        "rest": "absent",
        "uncertain": [],
    }
    assert (nothing.json()["message_code"], nothing.json()["draft"]) == (
        "mark-attendance.nothing",
        None,
    )
    assert sheet.json()["marks"] == []  # read, never saved


async def test_from_the_assistant_it_reads_marks_for_the_one_open_project(
    client: AsyncClient, owner: Owner
) -> None:
    farm, (meena,) = await farm_with(client, owner.account, "Meena")

    run = await ask(client, owner.account, "Meena half day")
    paid = await ask(client, owner.account, "Meena paid 500 for seeds")
    # A mark word but no worker for certain: the other skills' to answer.
    talk = await ask(client, owner.account, "show notifications that came today")
    await farm_with(client, owner.account)  # a second open project: which one?
    unsure = await ask(client, owner.account, "Meena half day")

    [action] = run["client_actions"]
    assert action["tool"] == "labor_attendance_draft"
    assert action["arguments"]["draft"]["project_id"] == farm
    assert action["arguments"]["draft"]["marks"][0]["worker_id"] == meena
    assert "labor_attendance_draft" not in {item["tool"] for item in paid["client_actions"]}
    assert "labor_attendance_draft" not in {item["tool"] for item in talk["client_actions"]}
    assert (unsure["message_code"], unsure["client_actions"]) == (
        "mark-attendance.which_project",
        [],
    )


async def test_an_account_without_labor_reaches_none_of_it(client: AsyncClient) -> None:
    stranger = await register(client)

    response = await client.post(
        f"{LABOR}/projects/0123456789abcdef01234567/attendance/read",
        headers=stranger.headers,
        json={"text": "everyone full day"},
    )
    run = await ask(client, stranger, "everyone full day")

    assert (response.status_code, response.json()["code"]) == (404, "MODULE_NOT_FOUND")
    assert "labor_attendance_draft" not in {item["tool"] for item in run["client_actions"]}


@respx.mock
async def test_with_ai_the_workers_are_read_first_then_the_draft_handed_over(
    agent_client: AgentClient,
) -> None:
    async with agent_client(providers=[PRIMARY]) as client:
        admin = await admin_account(client)
        owner = await owner_account(client, admin, "Meena")
        await give_consent(client, owner)
        farm, (ravi,) = await farm_with(client, owner, "Ravi")
        draft = {
            "project_id": farm,
            "marks": [{"worker_id": ravi, "name": "Ravi", "status": "full", "uncertain": []}],
        }
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("labor_day", {"project_id": farm})),
                calls(("labor_attendance_draft", {"draft": draft})),
                reply("Ravi: full day. Check it and save."),
            ]
        )
        response = await client.post(
            f"{LABOR}/projects/{farm}/attendance/read",
            headers=owner.headers,
            json={"text": "ரவி முழு நாள்", "input_type": "speech", "locale": "ta"},
        )
        [looked_up] = [m["content"] for m in sent(route, 1)["messages"] if m["role"] == "tool"]
        day = json.loads(looked_up)["result"]["day"]
        sheet = await client.get(
            f"{LABOR}/projects/{farm}/attendance", headers=owner.headers, params={"day": day}
        )

    body = response.json()
    assert (body["mode"], body["message"]) == ("agent", "Ravi: full day. Check it and save.")
    assert body["draft"]["marks"][0]["worker_id"] == ravi
    # The page's project reached the AI, which read the workers before marking anyone.
    assert farm in sent(route, 0)["messages"][0]["content"]
    assert [worker["name"] for worker in json.loads(looked_up)["result"]["workers"]] == ["Ravi"]
    assert sheet.json()["marks"] == []  # handed over, never saved
