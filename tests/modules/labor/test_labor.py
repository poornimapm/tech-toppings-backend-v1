"""Labor Management, through its API, as the spec's acceptance criteria say: an owner the
admin created works their own projects, workers, attendance, wages and payments; nothing of
theirs reaches another owner; every change is in the audit trail with its before and after."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.modules.labor.report_file import COLUMNS
from app.modules.labor.rules import rules
from tests.helpers import Account, api_operations, register
from tests.modules.labor.conftest import JPEG, LABOR, Owner, day_of

PROJECTS = f"{LABOR}/projects"
WORKERS = f"{LABOR}/workers"


async def project(client: AsyncClient, owner: Account, **fields: Any) -> dict[str, Any]:
    response = await client.post(
        PROJECTS, headers=owner.headers, json={"name": "North farm", **fields}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def worker(
    client: AsyncClient, owner: Account, project_id: str, **fields: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{PROJECTS}/{project_id}/workers",
        headers=owner.headers,
        json={"name": "Lakshmi", **fields},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def day_url(project_id: str, day: dt.date) -> str:
    return f"{PROJECTS}/{project_id}/attendance?day={day.isoformat()}"


async def mark(
    client: AsyncClient, owner: Account, project_id: str, day: dt.date, *marks: tuple[str, str]
) -> dict[str, Any]:
    response = await client.put(
        day_url(project_id, day),
        headers=owner.headers,
        json={"marks": [{"worker_id": who, "status": status} for who, status in marks]},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def audit(client: AsyncClient, owner: Account, **filters: str) -> list[dict[str, Any]]:
    response = await client.get(f"{LABOR}/audit", headers=owner.headers, params=filters)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


# ---------------------------------------------------------------------- access


async def test_only_accounts_given_labor_see_it_or_reach_it(
    client: AsyncClient, owner: Owner
) -> None:
    stranger = await register(client)

    mine = (await client.get("/v1/modules", headers=owner.account.headers)).json()
    theirs = (await client.get("/v1/modules", headers=stranger.headers)).json()
    refused = await client.get(PROJECTS, headers=stranger.headers)

    assert "labor" in {item["key"] for item in mine["items"]}
    assert "labor" not in {item["key"] for item in theirs["items"]}
    # Not there at all for them: as if no such module.
    assert (refused.status_code, refused.json()["code"]) == (404, "MODULE_NOT_FOUND")


async def test_one_owner_never_reaches_anothers_records(
    client: AsyncClient, owner: Owner, other_owner: Owner
) -> None:
    mine = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, mine["id"])
    base = f"{PROJECTS}/{mine['id']}"
    person = f"{WORKERS}/{lakshmi['id']}"
    intruder = other_owner.account.headers
    marks = {"marks": [{"worker_id": lakshmi["id"], "status": "full"}]}

    answers = [
        await client.get(base, headers=intruder),
        await client.patch(base, headers=intruder, json={"name": "Mine now"}),
        await client.get(person, headers=intruder),
        await client.get(f"{person}/photo", headers=intruder),
        await client.put(day_url(mine["id"], day_of(owner)), headers=intruder, json=marks),
        await client.post(
            f"{base}/payments",
            headers=intruder,
            json={"worker_id": lakshmi["id"], "amount_minor": 100},
        ),
    ]
    listed = (await client.get(PROJECTS, headers=intruder)).json()

    assert [answer.status_code for answer in answers] == [404] * len(answers)
    assert listed["total"] == 0
    assert await audit(client, other_owner.account) == []


# ---------------------------------------------------------------------- projects


async def test_projects_overlap_and_stay_open_until_closed(
    client: AsyncClient, owner: Owner
) -> None:
    first = await project(
        client, owner.account, name="Coffee farm, October", started_on="2026-10-01", kind="farm"
    )
    second = await project(
        client, owner.account, name="House help", started_on="2026-10-01", kind="household"
    )
    lakshmi = await worker(client, owner.account, first["id"])
    url = f"{PROJECTS}/{first['id']}"
    headers = owner.account.headers

    closed = await client.post(f"{url}/close", headers=headers, json={"note": "Season over"})
    refused = await client.put(
        day_url(first["id"], day_of(owner)),
        headers=headers,
        json={"marks": [{"worker_id": lakshmi["id"], "status": "full"}]},
    )
    renamed = await client.patch(url, headers=headers, json={"name": "Coffee farm"})
    reopened = await client.post(f"{url}/reopen", headers=headers, json={})
    listed = (await client.get(PROJECTS, headers=headers)).json()["items"]
    workers = (await client.get(f"{url}/workers", headers=headers)).json()

    assert {item["id"] for item in listed} == {first["id"], second["id"]}
    assert (closed.json()["status"], reopened.json()["status"]) == ("closed", "open")
    assert (refused.status_code, refused.json()["code"]) == (409, "LABOR_PROJECT_CLOSED")
    # Renaming keeps everything in it (a closed project's details may still be put right).
    assert renamed.json()["name"] == "Coffee farm"
    assert [item["id"] for item in workers["items"]] == [lakshmi["id"]]
    entries = await audit(client, owner.account, project_id=first["id"], entity="project")
    assert [entry["action"] for entry in entries] == ["reopened", "updated", "closed", "created"]
    closing = entries[2]
    assert closing["changes"] == [{"field": "status", "before": "open", "after": "closed"}]
    assert closing["note"] == "Season over"


# ---------------------------------------------------------------------- workers and wages


async def test_workers_get_numbers_and_the_projects_usual_wage(
    client: AsyncClient, owner: Owner
) -> None:
    farm = await project(client, owner.account)
    rates = await client.put(
        f"{PROJECTS}/{farm['id']}/rates",
        headers=owner.account.headers,
        json={
            "by_gender": True,
            "default_minor": 40000,
            "female_minor": 45000,
            "male_minor": 50000,
        },
    )

    lakshmi = await worker(client, owner.account, farm["id"], gender="female")
    ravi = await worker(client, owner.account, farm["id"], name="Ravi", gender="male")
    sam = await worker(client, owner.account, farm["id"], name="Sam")
    asha = await worker(client, owner.account, farm["id"], name="Asha", daily_wage_minor=10000)
    taken = await client.post(
        f"{PROJECTS}/{farm['id']}/workers",
        headers=owner.account.headers,
        json={"name": "Copy", "code": "W001"},
    )

    assert rates.status_code == 200, rates.text
    everyone = (lakshmi, ravi, sam, asha)
    assert [w["code"] for w in everyone] == ["W001", "W002", "W003", "W004"]
    assert [w["daily_wage_minor"] for w in everyone] == [45000, 50000, 40000, 10000]
    assert (taken.status_code, taken.json()["code"]) == (409, "LABOR_WORKER_CODE_TAKEN")
    [entry] = await audit(client, owner.account, entity="rates")
    assert {"field": "female_minor", "before": 0, "after": 45000} in entry["changes"]


async def test_a_wage_change_never_rewrites_the_past(client: AsyncClient, owner: Owner) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"], daily_wage_minor=30000)
    today = day_of(owner)
    yesterday = today - dt.timedelta(days=1)
    await mark(client, owner.account, farm["id"], yesterday, (lakshmi["id"], "full"))
    person = f"{WORKERS}/{lakshmi['id']}"

    changed = await client.put(
        f"{person}/wage",
        headers=owner.account.headers,
        json={"daily_wage_minor": 40000, "reason": "Harvest season"},
    )
    day_before = await mark(client, owner.account, farm["id"], yesterday, (lakshmi["id"], "half"))
    day_now = await mark(client, owner.account, farm["id"], today, (lakshmi["id"], "full"))
    history = (await client.get(f"{person}/wages", headers=owner.account.headers)).json()
    [entry] = await audit(client, owner.account, action="wage_changed")

    assert changed.json()["daily_wage_minor"] == 40000
    # Yesterday keeps its rate even when its mark changes; today is at the new one.
    assert [(m["rate_minor"], m["amount_minor"]) for m in day_before["marks"]] == [(30000, 15000)]
    assert [(m["rate_minor"], m["amount_minor"]) for m in day_now["marks"]] == [(40000, 40000)]
    assert [(h["from_minor"], h["to_minor"]) for h in history] == [(30000, 40000), (None, 30000)]
    assert entry["changes"] == [
        {"field": "daily_wage_minor", "before": 30000, "after": 40000},
        {"field": "effective_on", "before": None, "after": today.isoformat()},
    ]
    assert (entry["note"], entry["actor_name"]) == ("Harvest season", owner.name)


async def test_a_wage_takes_effect_from_its_day(client: AsyncClient, owner: Owner) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"], daily_wage_minor=30000)
    today = day_of(owner)
    days = [today - dt.timedelta(days=back) for back in (4, 3, 2, 1)]
    wage = f"{WORKERS}/{lakshmi['id']}/wage"
    headers = owner.account.headers

    async def new_wage(amount: int, start: dt.date) -> Any:
        body = {"daily_wage_minor": amount, "effective_on": start.isoformat()}
        return await client.put(wage, headers=headers, json=body)

    async def paid_on(day: dt.date) -> int:
        sheet = await mark(client, owner.account, farm["id"], day, (lakshmi["id"], "full"))
        return int(sheet["marks"][0]["rate_minor"])

    first_day = await paid_on(days[0])
    raised = await new_wage(40000, days[2])
    backfilled = await paid_on(days[1])  # before the raise's day: the wage then
    after_raise = await paid_on(days[3])
    lowered = await new_wage(35000, days[1])  # made later, from an earlier day: it outranks
    on_raise_day = await paid_on(days[2])
    future = await new_wage(50000, today + dt.timedelta(days=1))
    again = await new_wage(35000, days[3])  # every day from then on is at it already
    history = (await client.get(f"{WORKERS}/{lakshmi['id']}/wages", headers=headers)).json()
    sheets = [
        (await client.get(day_url(farm["id"], day), headers=headers)).json()["marks"][0]
        for day in days
    ]

    assert (first_day, backfilled, after_raise, on_raise_day) == (30000, 30000, 40000, 35000)
    assert [mark["rate_minor"] for mark in sheets] == [30000, 30000, 35000, 40000]  # kept
    assert (raised.json()["daily_wage_minor"], lowered.json()["daily_wage_minor"]) == (
        40000,
        35000,
    )
    assert (future.status_code, future.json()["code"]) == (422, "LABOR_WAGE_DATE_INVALID")
    assert again.status_code == 200
    assert [(h["from_minor"], h["to_minor"], h["effective_on"]) for h in history] == [
        (30000, 35000, days[1].isoformat()),
        (30000, 40000, days[2].isoformat()),
        (None, 30000, today.isoformat()),
    ]


async def test_a_workers_details_change_and_their_number_stays_theirs(
    client: AsyncClient, owner: Owner
) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"])
    ravi = await worker(client, owner.account, farm["id"], name="Ravi")
    url = f"{WORKERS}/{lakshmi['id']}"
    headers = owner.account.headers
    details = {"name": "Lakshmi K", "phone": "00000 11111"}

    changed = await client.patch(url, headers=headers, json=details)
    same = await client.patch(url, headers=headers, json=details)
    taken = await client.patch(url, headers=headers, json=details | {"code": ravi["code"]})
    entries = await audit(client, owner.account, entity="worker", action="updated")

    assert changed.status_code == 200, changed.text
    assert (changed.json()["name"], changed.json()["code"]) == ("Lakshmi K", lakshmi["code"])
    assert same.status_code == 200
    assert (taken.status_code, taken.json()["code"]) == (409, "LABOR_WORKER_CODE_TAKEN")
    [entry] = entries  # the change that changed nothing left no entry
    assert {change["field"] for change in entry["changes"]} == {"name", "phone"}


async def test_a_worker_is_made_inactive_never_removed(client: AsyncClient, owner: Owner) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"])
    today = day_of(owner)
    yesterday = today - dt.timedelta(days=1)
    await mark(client, owner.account, farm["id"], yesterday, (lakshmi["id"], "full"))
    headers = owner.account.headers

    off = await client.put(
        f"{WORKERS}/{lakshmi['id']}/active", headers=headers, json={"active": False}
    )
    now = await client.get(day_url(farm["id"], today), headers=headers)
    then = await client.get(day_url(farm["id"], yesterday), headers=headers)
    new_mark = await client.put(
        day_url(farm["id"], today),
        headers=headers,
        json={"marks": [{"worker_id": lakshmi["id"], "status": "full"}]},
    )

    assert off.json()["active"] is False
    assert now.json()["workers"] == []  # not on today's sheet
    assert [w["id"] for w in then.json()["workers"]] == [
        lakshmi["id"]
    ]  # but on the day they worked
    assert (new_mark.status_code, new_mark.json()["code"]) == (422, "LABOR_WORKER_UNKNOWN")


async def test_a_workers_photo_and_id_are_kept_small_and_private(
    client: AsyncClient, owner: Owner, other_owner: Owner
) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"])
    person = f"{WORKERS}/{lakshmi['id']}"
    image = owner.account.headers | {"Content-Type": "image/jpeg"}

    photo = await client.put(f"{person}/photo", headers=image, content=JPEG)
    card = await client.put(f"{person}/id-card", headers=image, content=JPEG + b"ID")
    shown = await client.get(f"{person}/photo", headers=owner.account.headers)
    not_an_image = await client.put(f"{person}/photo", headers=image, content=b"<svg>")
    too_large = await client.put(f"{person}/photo", headers=image, content=JPEG + bytes(300_000))
    stolen = await client.get(f"{person}/id-card", headers=other_owner.account.headers)
    removed = await client.delete(f"{person}/id-card", headers=owner.account.headers)
    gone = await client.get(f"{person}/id-card", headers=owner.account.headers)

    assert photo.status_code == 200, photo.text
    assert (photo.json()["has_photo"], card.json()["has_id_card"]) == (True, True)
    assert shown.content == JPEG
    assert shown.headers["content-type"] == "image/jpeg"
    assert shown.headers["cache-control"] == "private, no-store"
    assert (not_an_image.status_code, too_large.status_code, stolen.status_code) == (415, 413, 404)
    assert (removed.json()["has_id_card"], gone.status_code) == (False, 404)
    actions = [entry["action"] for entry in await audit(client, owner.account, entity="worker")]
    assert "photo_changed" in actions
    assert "id_card_removed" in actions
    entry = next(e for e in await audit(client, owner.account) if e["action"] == "id_card_changed")
    assert entry["changes"] == []  # that it changed is said; the picture never is


# ---------------------------------------------------------------------- attendance


async def test_a_day_is_marked_in_one_go_and_summed(client: AsyncClient, owner: Owner) -> None:
    farm = await project(client, owner.account)
    people = [
        await worker(client, owner.account, farm["id"], name=name, daily_wage_minor=wage)
        for name, wage in (("A", 30000), ("B", 30101), ("C", 50000), ("D", 40000))
    ]
    today = day_of(owner)

    sheet = await mark(
        client,
        owner.account,
        farm["id"],
        today,
        (people[0]["id"], "full"),
        (people[1]["id"], "half"),
        (people[2]["id"], "absent"),
    )
    again = await mark(client, owner.account, farm["id"], today, (people[0]["id"], "half"))
    future = await client.put(
        day_url(farm["id"], today + dt.timedelta(days=1)),
        headers=owner.account.headers,
        json={"marks": [{"worker_id": people[0]["id"], "status": "full"}]},
    )

    amounts = {m["worker_id"]: m["amount_minor"] for m in sheet["marks"]}
    assert [amounts[p["id"]] for p in people[:3]] == [30000, 15051, 0]  # half of ₹301.01 rounds up
    assert sheet["counts"] == {"full": 1, "half": 1, "absent": 1, "unmarked": 1}
    assert sheet["total_minor"] == 45051
    # The same worker and day is one record, changed: never a second.
    assert len(again["marks"]) == 3
    assert again["total_minor"] == 15000 + 15051
    assert (future.status_code, future.json()["code"]) == (422, "LABOR_DAY_IN_FUTURE")
    changed = next(
        e
        for e in await audit(client, owner.account, entity="attendance")
        if e["action"] == "changed"
    )
    assert {"field": "status", "before": "full", "after": "half"} in changed["changes"]


async def test_a_mark_kept_offline_never_overwrites_a_newer_one(
    client: AsyncClient, owner: Owner
) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"])
    ravi = await worker(client, owner.account, farm["id"], name="Ravi")
    today = day_of(owner)
    url = day_url(farm["id"], today)
    headers = owner.account.headers

    def later(who: str, status: str, saw: str) -> dict[str, Any]:
        return {"marks": [{"worker_id": who, "status": status, "expected": saw}]}

    # Tapped "half" offline while the day was unmarked; meanwhile marked "full" online.
    await mark(client, owner.account, farm["id"], today, (lakshmi["id"], "full"))
    old = await client.put(url, headers=headers, json=later(lakshmi["id"], "half", "unmarked"))
    # Tapped after seeing "full": it goes; sent twice, it changes nothing more.
    seen = await client.put(url, headers=headers, json=later(lakshmi["id"], "half", "full"))
    again = await client.put(url, headers=headers, json=later(lakshmi["id"], "half", "full"))
    first = await client.put(url, headers=headers, json=later(ravi["id"], "full", "unmarked"))
    sheet = (await client.get(url, headers=headers)).json()

    assert (old.status_code, old.json()["code"]) == (409, "LABOR_MARK_CHANGED")
    assert old.json()["details"] == {"worker_id": lakshmi["id"], "now": "full"}
    assert (seen.status_code, again.status_code, first.status_code) == (200, 200, 200)
    assert {m["worker_id"]: m["status"] for m in sheet["marks"]} == {
        lakshmi["id"]: "half",
        ravi["id"]: "full",
    }
    entries = await audit(client, owner.account, entity="attendance")
    # Lakshmi: marked, then changed once (the refused one and the repeat left nothing).
    assert sorted(e["action"] for e in entries) == ["changed", "marked", "marked"]


# ---------------------------------------------------------------------- payments and reports


async def test_partial_payments_balances_and_a_voided_one(
    client: AsyncClient, owner: Owner
) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"], daily_wage_minor=40000)
    today = day_of(owner)
    for back in range(3):
        day = today - dt.timedelta(days=back)
        await mark(client, owner.account, farm["id"], day, (lakshmi["id"], "full"))
    url = f"{PROJECTS}/{farm['id']}/payments"
    headers = owner.account.headers
    keyed = headers | {"Idempotency-Key": "payment-0001"}
    first_payment = {"worker_id": lakshmi["id"], "amount_minor": 50000, "method": "upi"}

    first = await client.post(url, headers=keyed, json=first_payment)
    repeat = await client.post(url, headers=keyed, json=first_payment)
    mistake = await client.post(
        url, headers=headers, json={"worker_id": lakshmi["id"], "amount_minor": 99900}
    )
    void = f"{LABOR}/payments/{mistake.json()['id']}/void"
    voided = await client.post(void, headers=headers, json={"reason": "Typed wrong"})
    again = await client.post(void, headers=headers, json={"reason": "Twice"})
    week = {"from": (today - dt.timedelta(days=6)).isoformat(), "to": today.isoformat()}
    report = (
        await client.get(f"{PROJECTS}/{farm['id']}/report", headers=headers, params=week)
    ).json()
    history = (await client.get(url, headers=headers)).json()

    assert (first.status_code, repeat.status_code) == (201, 201)
    assert repeat.json()["id"] == first.json()["id"]  # sent twice, kept once
    assert voided.json()["void_reason"] == "Typed wrong"
    assert (again.status_code, again.json()["code"]) == (409, "LABOR_PAYMENT_VOIDED")
    [mine] = report["workers"]
    assert (mine["earned_minor"], mine["paid_minor"], mine["owed_minor"]) == (120000, 50000, 70000)
    assert mine["owed_to_date_minor"] == 70000
    assert history["total"] == 2  # the voided one stays in the history
    actions = {entry["action"] for entry in await audit(client, owner.account, entity="payment")}
    assert actions == {"paid", "voided"}


async def test_a_report_names_its_workers_and_adds_up_to_date(
    client: AsyncClient, owner: Owner
) -> None:
    farm = await project(client, owner.account)  # starts today
    lakshmi = await worker(client, owner.account, farm["id"], daily_wage_minor=40000)
    ravi = await worker(client, owner.account, farm["id"], name="Ravi", daily_wage_minor=30000)
    today = day_of(owner)
    before_start = today - dt.timedelta(days=1)
    await mark(client, owner.account, farm["id"], before_start, (lakshmi["id"], "full"))
    await mark(client, owner.account, farm["id"], today, (lakshmi["id"], "half"))
    headers = owner.account.headers
    payment = {"worker_id": lakshmi["id"], "amount_minor": 10000}
    await client.post(f"{PROJECTS}/{farm['id']}/payments", headers=headers, json=payment)
    only_today = {"from": today.isoformat(), "to": today.isoformat()}

    report = (
        await client.get(f"{PROJECTS}/{farm['id']}/report", headers=headers, params=only_today)
    ).json()
    [paid] = await audit(client, owner.account, entity="payment")

    [row] = report["workers"]  # Ravi has no days and no money: not in it
    assert (row["code"], row["name"]) == (lakshmi["code"], "Lakshmi")
    assert (row["earned_minor"], row["paid_minor"]) == (20000, 10000)  # today's
    # Every day up to today, the day before the project's start date too: they add up.
    to_date = (row["earned_to_date_minor"], row["paid_to_date_minor"], row["owed_to_date_minor"])
    assert to_date == (60000, 10000, 50000)
    assert ravi["id"] not in {item["worker_id"] for item in report["workers"]}
    # The payment's entry says whom it paid in words, not by an id.
    assert paid["labels"] == {lakshmi["id"]: f"Lakshmi ({lakshmi['code']})"}


async def test_a_report_downloads_as_a_file_with_each_day_and_payment(
    client: AsyncClient, owner: Owner, admin: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    farm = await project(client, owner.account)
    tamil = await worker(client, owner.account, farm["id"], name="லட்சுமி", daily_wage_minor=40000)
    tricky = await worker(
        client, owner.account, farm["id"], name="=HYPERLINK(1)", daily_wage_minor=30101
    )
    today = day_of(owner)
    yesterday = today - dt.timedelta(days=1)
    await mark(client, owner.account, farm["id"], yesterday, (tamil["id"], "full"))
    await mark(client, owner.account, farm["id"], yesterday, (tricky["id"], "half"))
    await mark(client, owner.account, farm["id"], today, (tamil["id"], "absent"))
    headers = owner.account.headers
    url = f"{PROJECTS}/{farm['id']}/payments"
    await client.post(url, headers=headers, json={"worker_id": tamil["id"], "amount_minor": 10000})
    mistake = await client.post(
        url, headers=headers, json={"worker_id": tamil["id"], "amount_minor": 99900}
    )
    void = f"{LABOR}/payments/{mistake.json()['id']}/void"
    await client.post(void, headers=headers, json={"reason": "Typo"})
    period = {"from": yesterday.isoformat(), "to": today.isoformat(), "group": "day"}
    file_url = f"{PROJECTS}/{farm['id']}/report.csv"

    response = await client.get(file_url, headers=headers, params=period)
    as_admin = await client.get(
        f"/v1/admin/labor/projects/{farm['id']}/report.csv", headers=admin.headers, params=period
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    name = f"labor-report-{yesterday.isoformat()}-to-{today.isoformat()}.csv"
    assert response.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert response.text.startswith("﻿")
    header, *rows = list(csv.reader(io.StringIO(response.text.removeprefix("﻿"))))
    assert header == list(COLUMNS)
    by_section: dict[str, list[list[str]]] = {}
    for row in rows:
        by_section.setdefault(row[0], []).append(row)
    [total] = by_section["total"]
    assert total[1:] == [yesterday.isoformat(), "", "", "", "1", "1", "1", "1.5",
                         "550.51", "100.00", "450.51", ""]  # fmt: skip
    assert [row[1] for row in by_section["period"]] == [yesterday.isoformat(), today.isoformat()]
    # The guard on a name a spreadsheet would run; Tamil kept as it is.
    assert {row[3] for row in by_section["worker"]} == {"லட்சுமி", "'=HYPERLINK(1)"}
    assert [(row[1], row[4], row[8], row[9]) for row in by_section["mark"]] == [
        (yesterday.isoformat(), "full", "1", "400.00"),
        (yesterday.isoformat(), "half", "0.5", "150.51"),
        (today.isoformat(), "absent", "0", "0.00"),
    ]
    [paid] = by_section["payment"]  # the voided one is left out, as in the sums
    assert (paid[3], paid[10]) == ("லட்சுமி", "100.00")
    assert as_admin.status_code == 200
    assert as_admin.text == response.text

    smaller = rules().model_copy(
        update={"reports": rules().reports.model_copy(update={"export_max_rows": 3})}
    )
    monkeypatch.setattr("app.modules.labor.reports.rules", lambda: smaller)
    too_many = await client.get(file_url, headers=headers, params=period)  # 3 days, 1 payment
    assert (too_many.status_code, too_many.json()["code"]) == (422, "LABOR_EXPORT_TOO_LARGE")
    assert too_many.json()["details"] == {"max": 3}


async def test_a_workers_history_shows_each_day_and_payment(
    client: AsyncClient, owner: Owner, other_owner: Owner
) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"], daily_wage_minor=40000)
    headers = owner.account.headers
    today = day_of(owner)
    days = [today - dt.timedelta(days=back) for back in (2, 1, 0)]
    await mark(client, owner.account, farm["id"], days[0], (lakshmi["id"], "full"))
    await mark(client, owner.account, farm["id"], days[1], (lakshmi["id"], "absent"))
    raise_from = {"daily_wage_minor": 50000, "effective_on": today.isoformat()}
    await client.put(f"{WORKERS}/{lakshmi['id']}/wage", headers=headers, json=raise_from)
    await mark(client, owner.account, farm["id"], days[2], (lakshmi["id"], "half"))
    url = f"{PROJECTS}/{farm['id']}/payments"
    await client.post(
        url, headers=headers, json={"worker_id": lakshmi["id"], "amount_minor": 30000}
    )
    mistake = await client.post(
        url, headers=headers, json={"worker_id": lakshmi["id"], "amount_minor": 99900}
    )
    await client.post(
        f"{LABOR}/payments/{mistake.json()['id']}/void", headers=headers, json={"reason": "Typo"}
    )
    period = {"from": days[0].isoformat(), "to": today.isoformat()}
    history_url = f"{WORKERS}/{lakshmi['id']}/history"

    history = await client.get(history_url, headers=headers, params=period)
    theirs = await client.get(history_url, headers=other_owner.account.headers, params=period)
    backwards = await client.get(
        history_url, headers=headers, params={"from": period["to"], "to": period["from"]}
    )

    assert history.status_code == 200, history.text
    body = history.json()
    assert [(d["day"], d["status"], d["amount_minor"]) for d in body["days"]] == [
        (today.isoformat(), "half", 25000),  # at the new wage
        (days[1].isoformat(), "absent", 0),
        (days[0].isoformat(), "full", 40000),  # still at the old one
    ]
    assert [p["voided_at"] is None for p in body["payments"]] == [False, True]
    assert (body["earned_minor"], body["paid_minor"]) == (65000, 30000)
    assert body["worker"]["id"] == lakshmi["id"]
    assert theirs.status_code == 404  # another owner's worker is not there for them
    assert (backwards.status_code, backwards.json()["code"]) == (422, "LABOR_PERIOD_INVALID")


@pytest.mark.parametrize("group", ["day", "week", "month"])
async def test_reports_sum_by_day_week_and_month(
    client: AsyncClient, owner: Owner, group: str
) -> None:
    farm = await project(client, owner.account)
    lakshmi = await worker(client, owner.account, farm["id"], daily_wage_minor=20000)
    today = day_of(owner)
    days = [today - dt.timedelta(days=back) for back in range(10)]
    for day in days:
        status = "full" if day.day % 2 else "half"
        await mark(client, owner.account, farm["id"], day, (lakshmi["id"], status))

    report = (
        await client.get(
            f"{PROJECTS}/{farm['id']}/report",
            headers=owner.account.headers,
            params={"from": days[-1].isoformat(), "to": today.isoformat(), "group": group},
        )
    ).json()

    full = sum(1 for day in days if day.day % 2)
    assert report["total"]["earned_minor"] == full * 20000 + (10 - full) * 10000
    assert report["total"]["days"] == full + (10 - full) / 2
    periods = report["periods"]
    assert sum(period["earned_minor"] for period in periods) == report["total"]["earned_minor"]
    starts = [dt.date.fromisoformat(period["start"]) for period in periods]
    if group == "week":
        assert all(start.weekday() == 0 for start in starts)
    if group == "month":
        assert all(start.day == 1 for start in starts)
    if group == "day":
        assert len(starts) == len(days)


async def test_a_report_spans_a_limited_period(client: AsyncClient, owner: Owner) -> None:
    farm = await project(client, owner.account)
    today = day_of(owner)

    backwards = await client.get(
        f"{PROJECTS}/{farm['id']}/report",
        headers=owner.account.headers,
        params={"from": today.isoformat(), "to": (today - dt.timedelta(days=1)).isoformat()},
    )
    too_long = await client.get(
        f"{PROJECTS}/{farm['id']}/report",
        headers=owner.account.headers,
        params={"from": (today - dt.timedelta(days=2000)).isoformat(), "to": today.isoformat()},
    )

    assert (backwards.status_code, too_long.status_code) == (422, 422)
    assert too_long.json()["code"] == "LABOR_PERIOD_INVALID"


async def test_the_overview_shows_today_across_open_projects(
    client: AsyncClient, owner: Owner
) -> None:
    farm = await project(client, owner.account)
    home = await project(client, owner.account, name="House help")
    a = await worker(client, owner.account, farm["id"], daily_wage_minor=30000)
    await worker(client, owner.account, farm["id"], name="B", daily_wage_minor=30000)
    c = await worker(client, owner.account, home["id"], name="C", daily_wage_minor=50000)
    await mark(client, owner.account, farm["id"], day_of(owner), (a["id"], "full"))
    await mark(client, owner.account, home["id"], day_of(owner), (c["id"], "half"))

    overview = (await client.get(f"{LABOR}/overview", headers=owner.account.headers)).json()

    assert (overview["open_projects"], overview["workers"]) == (2, 3)
    assert overview["today_minor"] == 30000 + 25000
    by_name = {row["name"]: row for row in overview["projects"]}
    assert by_name["North farm"]["counts"] == {"full": 1, "half": 0, "absent": 0, "unmarked": 1}
    assert overview["owed_minor"] == 55000
    assert overview["recent"][0]["entity"] == "attendance"
    tile = (await client.get("/v1/modules/labor", headers=owner.account.headers)).json()
    assert tile["stat"]["value"] == 2  # two projects open


# ---------------------------------------------------------------------- admin


async def test_the_admin_sees_every_owner_and_their_audit(
    client: AsyncClient, admin: Account, owner: Owner, other_owner: Owner
) -> None:
    mine = await project(client, owner.account)
    await project(client, other_owner.account, name="Office cleaning")

    owners = (await client.get("/v1/admin/labor/owners", headers=admin.headers)).json()
    projects = (await client.get("/v1/admin/labor/projects", headers=admin.headers)).json()
    trail = (
        await client.get(
            "/v1/admin/labor/audit", headers=admin.headers, params={"owner": owner.account.user_id}
        )
    ).json()
    refused = await client.get("/v1/admin/labor/owners", headers=owner.account.headers)

    assert {row["email"] for row in owners} >= {owner.account.email, other_owner.account.email}
    assert {row["name"] for row in projects["items"]} >= {"North farm", "Office cleaning"}
    assert [entry["entity_id"] for entry in trail["items"]] == [mine["id"]]
    assert refused.status_code == 403


async def test_owners_are_made_by_the_admin_and_given_labor(
    client: AsyncClient, admin: Account, owner: Owner
) -> None:
    taken = await client.post(
        "/v1/admin/users",
        headers=admin.headers,
        json={"email": owner.account.email, "name": "Again", "modules": ["labor"]},
    )
    not_grantable = await client.post(
        "/v1/admin/users",
        headers=admin.headers,
        json={"email": "someone-new@example.com", "name": "New", "modules": ["expenses"]},
    )
    revoked = await client.put(
        f"/v1/admin/users/{owner.account.user_id}/modules/labor",
        headers=admin.headers,
        json={"granted": False},
    )
    hidden = await client.get(PROJECTS, headers=owner.account.headers)
    regranted = await client.put(
        f"/v1/admin/users/{owner.account.user_id}/modules/labor",
        headers=admin.headers,
        json={"granted": True},
    )

    assert (taken.status_code, not_grantable.status_code) == (409, 422)
    assert (revoked.json()["modules"], regranted.json()["modules"]) == ([], ["labor"])
    assert hidden.status_code == 404
    assert (await client.get(PROJECTS, headers=owner.account.headers)).status_code == 200


async def test_the_audit_trail_has_no_way_to_change_it(
    app: FastAPI,
    client: AsyncClient,  # noqa: ARG001 - started, so the modules' routes are mounted
) -> None:
    audit_routes = [
        (method, path)
        for method, path in api_operations(app)
        if "/labor/" in path and "audit" in path
    ]

    assert audit_routes
    assert {method for method, _ in audit_routes} == {"GET"}  # read only, owner's and admin's
