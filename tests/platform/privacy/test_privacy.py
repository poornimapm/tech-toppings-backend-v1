"""``GET /v1/me/export`` and ``DELETE /v1/me``: everything of mine and nothing of anyone else's,
nothing secret in the file, and an erasure that leaves nothing of mine behind."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from beanie import Document, PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.platform.audit.models import AuditEntry
from app.platform.auth.allowlist import AllowedEmail
from app.platform.privacy.service import AccountData
from app.platform.users.models import User
from tests.helpers import (
    REFRESH_COOKIE,
    Account,
    FrozenClock,
    build_app,
    build_settings,
    login,
    register,
    running_client,
    unique_email,
)
from tests.modules.expenses.conftest import add
from tests.platform.admin.conftest import admin_account
from tests.platform.auth.conftest import code_now, earlier_clock, turn_on

EXPORT = "/v1/me/export"


@pytest.fixture
def clock() -> FrozenClock:
    return earlier_clock()  # two-step sign-in tests move it forward


async def exported(client: AsyncClient, account: Account) -> dict[str, Any]:
    response = await client.get(EXPORT, headers=account.headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith('attachment; filename="my-data-')
    return dict(json.loads(response.content))


async def delete(client: AsyncClient, account: Account, **body: Any) -> Response:
    return await client.request(
        "DELETE",
        "/v1/me",
        headers=account.headers,
        json={"confirm_email": account.email, "password": account.password, **body},
    )


async def signed_in_again(client: AsyncClient, account: Account) -> Account:
    token = (await login(client, account.email)).json()["access_token"]
    return Account(account.email, account.password, token, account.user_id)


async def leftovers(app: FastAPI, user_id: str) -> dict[str, int]:
    """How many records of each collection still name this user."""
    found = {}
    documents: list[type[Document]] = app.state.documents
    for doc in (doc for doc in documents if "user_id" in doc.model_fields):
        count = await doc.get_pymongo_collection().count_documents(
            {"user_id": PydanticObjectId(user_id)}
        )
        if count:
            found[doc.get_collection_name()] = count
    return found


# ---------------------------------------------------------------------- export


async def test_the_export_holds_mine_and_nothing_secret(client: AsyncClient) -> None:
    asha, bob = await register(client), await register(client, name="Bob")
    await add(client, asha, title="Tea")
    await add(client, bob, title="Bob's lunch")

    data = await exported(client, asha)
    assert data["format"] == 1
    assert data["account"]["email"] == asha.email
    for secret in User.PRIVATE_FIELDS:
        assert secret not in data["account"]
    sessions = data["collections"]["auth_sessions"]
    assert sessions
    assert all("token_hash" not in item for item in sessions)
    titles = [item["title"] for item in data["collections"]["expenses"]]
    assert titles == ["Tea"]  # nothing of Bob's
    assert data["collections"]["expense_revisions"][0]["user_id"] == asha.user_id
    assert "account.exported" in [item["action"] for item in data["collections"]["audit_logs"]]

    stored = await User.get(PydanticObjectId(asha.user_id))
    assert stored is not None
    raw = json.dumps(data)
    assert str(stored.password_hash) not in raw
    assert "$argon2" not in raw


async def test_the_export_says_what_an_admin_did_but_not_who_or_from_where(
    client: AsyncClient,
) -> None:
    admin, asha = await admin_account(client), await register(client)
    revoked = await client.post(
        f"/v1/admin/users/{asha.user_id}/revoke-sessions", headers=admin.headers
    )
    assert revoked.status_code == 200
    asha = await signed_in_again(client, asha)

    trail = (await exported(client, asha))["collections"]["audit_logs"]
    by_admin = next(item for item in trail if item["action"] == "user.sessions_revoked")
    assert not {"actor_id", "ip", "request_id"} & set(by_admin)
    assert by_admin["details"] == {"sessions": 1}
    raw = json.dumps(trail)
    assert admin.user_id not in raw


async def test_a_large_export_is_still_one_valid_file(client: AsyncClient) -> None:
    asha = await register(client)
    many = [{"user_id": PydanticObjectId(asha.user_id), "n": n} for n in range(1201)]
    await User.get_pymongo_collection().database["expense_saved_filters"].insert_many(many)

    data = await exported(client, asha)
    assert [item["n"] for item in data["collections"]["expense_saved_filters"]] == list(range(1201))


# ---------------------------------------------------------------------- erasure


async def test_deleting_takes_the_email_and_password_then_leaves_nothing(
    client: AsyncClient, app: FastAPI
) -> None:
    asha, bob = await register(client), await register(client, name="Bob")
    await add(client, asha, title="Tea")
    await add(client, bob, title="Bob's lunch")
    assert await leftovers(app, asha.user_id)

    typo = await delete(client, asha, confirm_email="someone@example.com")
    assert (typo.status_code, typo.json()["code"]) == (422, "ACCOUNT_CONFIRM_MISMATCH")
    wrong = await delete(client, asha, password="Wrong-Password-1")
    assert (wrong.status_code, wrong.json()["code"]) == (422, "AUTH_CURRENT_PASSWORD_INVALID")

    gone = await delete(client, asha, confirm_email=asha.email.upper())
    assert gone.status_code == 204
    assert f'{REFRESH_COOKIE}=""' in gone.headers["set-cookie"]
    assert await User.get(PydanticObjectId(asha.user_id)) is None
    assert await leftovers(app, asha.user_id) == {}
    assert await leftovers(app, bob.user_id)  # Bob's untouched

    assert (await client.get("/v1/me", headers=asha.headers)).status_code == 401
    assert (await login(client, asha.email)).json()["code"] == "AUTH_INVALID_CREDENTIALS"
    entry = await AuditEntry.find_one(
        {"owner_id": PydanticObjectId(asha.user_id), "action": "account.deleted"}
    )
    assert entry is not None
    assert entry.details["erased"]["expenses"] == 1
    assert (await register(client, email=asha.email)).user_id != asha.user_id  # free to return


async def test_an_invitation_to_register_goes_with_the_account(client: AsyncClient) -> None:
    admin = await admin_account(client)
    email = unique_email("invited")
    invited = await client.post(
        "/v1/admin/allowed-emails", headers=admin.headers, json={"email": email}
    )
    assert invited.status_code == 201
    asha = await register(client, email=email)

    assert (await delete(client, asha)).status_code == 204
    assert await AllowedEmail.find_one({"email": email}) is None


async def test_a_deletion_that_failed_halfway_can_be_done_again(
    client: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    asha = await register(client)
    await add(client, asha, title="Tea")
    sweep = AccountData._sweep

    async def dying_after_it(self: AccountData) -> dict[str, int]:
        await sweep(self)  # the records are gone, then the server dies before the account is
        raise RuntimeError("simulated crash halfway through an erasure")

    monkeypatch.setattr(AccountData, "_sweep", dying_after_it)
    assert (await delete(client, asha)).status_code == 500
    monkeypatch.setattr(AccountData, "_sweep", sweep)

    assert (await client.get("/v1/me", headers=asha.headers)).status_code == 200  # still works
    assert (await delete(client, asha)).status_code == 204
    assert await User.get(PydanticObjectId(asha.user_id)) is None
    assert await leftovers(app, asha.user_id) == {}


async def test_with_two_step_sign_in_it_takes_a_code_too(
    client: AsyncClient, clock: FrozenClock
) -> None:
    on = await turn_on(client, clock)
    without = await delete(client, on.account)
    assert without.json()["code"] == "AUTH_TWO_FACTOR_CODE_INVALID"
    assert (await delete(client, on.account, code=code_now(on.secret, clock))).status_code == 204


@pytest.fixture
async def own_client(own_mongo_uri: str, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    async with running_client(build_app(build_settings(own_mongo_uri), clock)) as client:
        yield client


async def test_the_only_admin_cannot_leave(own_client: AsyncClient) -> None:
    first = await admin_account(own_client)
    alone = await delete(own_client, first)
    assert (alone.status_code, alone.json()["code"]) == (409, "ACCOUNT_LAST_ADMIN")

    await admin_account(own_client)
    assert (await delete(own_client, first)).status_code == 204


async def test_two_admins_leaving_at_once_leave_one(own_client: AsyncClient) -> None:
    first, second = await admin_account(own_client), await admin_account(own_client)

    answers = await asyncio.gather(delete(own_client, first), delete(own_client, second))

    assert {answer.status_code for answer in answers} <= {204, 409}
    assert await User.find({"role": "admin", "deleted_at": None}).count() >= 1
