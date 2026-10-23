"""``/v1/admin/audit-logs``: filters by whole UTC days, pages, and refuses a span it can't
read; and what an entry keeps of the details it is given (never a secret)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.audit.models import AuditEntry
from app.platform.audit.service import REDACTED, redacted
from tests.helpers import Account
from tests.platform.admin.conftest import admin_account, audit_trail

AUDIT = "/v1/admin/audit-logs"
DAY = datetime(2026, 3, 10, tzinfo=UTC)


async def written(admin: Account, action: str, *moments: datetime) -> None:
    await AuditEntry.insert_many(
        [
            AuditEntry(
                at=moment,
                action=action,
                actor_id=PydanticObjectId(admin.user_id),
                entity="test",
                purge_at=moment + timedelta(days=1),
            )
            for moment in moments
        ]
    )


async def test_a_day_runs_from_utc_midnight_to_midnight(client: AsyncClient) -> None:
    admin = await admin_account(client)
    action = f"test.{uuid.uuid4().hex[:8]}"
    edge = timedelta(microseconds=1000)
    await written(
        admin, action, DAY - edge, DAY, DAY + timedelta(days=1) - edge, DAY + timedelta(1)
    )

    day = DAY.date().isoformat()
    found = await audit_trail(client, admin, action=action, **{"from": day, "to": day})
    utc = sorted(datetime.fromisoformat(item["at"]).replace(tzinfo=None) for item in found)
    assert utc == [moment.replace(tzinfo=None) for moment in (DAY, DAY + timedelta(1) - edge)]


async def test_pages_add_up_to_the_total(client: AsyncClient) -> None:
    admin = await admin_account(client)
    action = f"test.{uuid.uuid4().hex[:8]}"
    await written(admin, action, *(DAY + timedelta(minutes=n) for n in range(5)))

    async def page(number: int) -> dict[str, Any]:
        params: dict[str, str | int] = {"action": action, "page": number, "page_size": 3}
        return dict((await client.get(AUDIT, headers=admin.headers, params=params)).json())

    first, second = await page(1), await page(2)
    assert (first["total"], second["total"]) == (5, 5)
    ids = [item["id"] for item in [*first["items"], *second["items"]]]
    assert len(ids) == len(set(ids)) == 5


@pytest.mark.parametrize("span", [{"from": "2026-03-10", "to": "2026-03-01"}, {"to": "9999-12-31"}])
async def test_a_span_it_cannot_read_is_a_422(client: AsyncClient, span: dict[str, str]) -> None:
    admin = await admin_account(client)
    refused = await client.get(AUDIT, headers=admin.headers, params=span)
    assert refused.status_code == 422


def test_secrets_are_left_out_at_any_depth() -> None:
    details = {
        "password": "x",
        "from": "user",
        "nested": {"api_key": "y", "kept": 1},
        "items": [{"token": "z", "name": "a"}, "plain", ("t", {"otp": "1"})],
    }
    assert redacted(details) == {
        "password": REDACTED,
        "from": "user",
        "nested": {"api_key": REDACTED, "kept": 1},
        "items": [{"token": REDACTED, "name": "a"}, "plain", ["t", {"otp": REDACTED}]],
    }
