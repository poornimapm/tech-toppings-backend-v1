"""Two-step sign-in: turning it on and off, signing in with a code or a recovery code, and
everything that must not work (replays, stale challenges, guessing, a server without a key)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from beanie import PydanticObjectId
from cryptography.fernet import Fernet
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.platform.audit.models import AuditEntry
from app.platform.users.models import User
from tests.helpers import (
    PASSWORD,
    Account,
    FrozenClock,
    auth_settings,
    bearer,
    build_app,
    build_settings,
    login,
    register,
    running_client,
)
from tests.platform.admin.conftest import admin_account
from tests.platform.auth.conftest import TOTP, code_now, earlier_clock, turn_on


@pytest.fixture
def clock() -> FrozenClock:
    return earlier_clock()


async def challenge(client: AsyncClient, account: Account) -> str:
    response = await login(client, account.email)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["two_factor_required"] is True
    assert "access_token" not in body
    assert not response.cookies.get("tt_refresh")  # no session until the code
    return str(body["challenge_token"])


async def finish(client: AsyncClient, token: str, code: str) -> Response:
    return await client.post("/v1/auth/login/totp", json={"challenge_token": token, "code": code})


async def trail(user_id: str, action: str) -> list[AuditEntry]:
    return await AuditEntry.find(
        {"owner_id": PydanticObjectId(user_id), "action": action}
    ).to_list()


async def stored(user_id: str) -> User:
    user = await User.get(PydanticObjectId(user_id))
    assert user is not None
    return user


# ---------------------------------------------------------------------- turning it on


async def test_setup_then_a_code_turns_it_on(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    status = (await client.get(TOTP, headers=account.headers)).json()
    assert status == {
        "available": True,
        "enabled": False,
        "enabled_at": None,
        "recovery_codes_left": 0,
    }
    setup = (await client.post(f"{TOTP}/setup", headers=account.headers)).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    assert f"secret={setup['secret']}" in setup["otpauth_uri"]
    # Set up but not confirmed: still off, and sign-in still takes the password alone.
    assert (await client.get(TOTP, headers=account.headers)).json()["enabled"] is False
    assert "access_token" in (await login(client, account.email)).json()

    async def enable(**body: Any) -> Response:
        return await client.post(f"{TOTP}/enable", headers=account.headers, json=body)

    code = code_now(setup["secret"], clock)
    no_password = await enable(code=code)
    assert no_password.json()["code"] == "AUTH_CURRENT_PASSWORD_INVALID"
    wrong = await enable(password=PASSWORD, code="000000")
    assert wrong.status_code == 422
    assert wrong.json()["code"] == "AUTH_TWO_FACTOR_CODE_INVALID"

    enabled = await enable(password=PASSWORD, code=f"{code[:3]} {code[3:]}")  # as apps show it
    assert enabled.status_code == 200, enabled.text
    codes = enabled.json()["recovery_codes"]
    assert len(codes) == len(set(codes)) == 10

    status = (await client.get(TOTP, headers=account.headers)).json()
    assert (status["enabled"], status["recovery_codes_left"]) == (True, 10)
    assert (await client.get("/v1/me", headers=account.headers)).json()["two_factor"] is True
    user = await stored(account.user_id)
    assert user.totp_secret is not None
    assert setup["secret"] not in user.totp_secret  # kept encrypted
    assert not set(codes) & set(user.recovery_codes)  # kept hashed
    assert len(await trail(account.user_id, "two_factor.enabled")) == 1


async def test_it_turns_on_once_and_only_after_setup(
    client: AsyncClient, clock: FrozenClock
) -> None:
    body = {"password": PASSWORD, "code": "123456"}
    fresh = await register(client)
    early = await client.post(f"{TOTP}/enable", headers=fresh.headers, json=body)
    assert (early.status_code, early.json()["code"]) == (409, "AUTH_TWO_FACTOR_NOT_SET_UP")

    on = await turn_on(client, clock)
    for path, sent in ((f"{TOTP}/setup", None), (f"{TOTP}/enable", body)):
        again = await client.post(path, headers=on.account.headers, json=sent)
        assert (again.status_code, again.json()["code"]) == (409, "AUTH_TWO_FACTOR_ALREADY_ON")


# ---------------------------------------------------------------------- signing in


async def test_sign_in_asks_for_a_code_and_takes_it_once(
    client: AsyncClient, clock: FrozenClock
) -> None:
    on = await turn_on(client, clock)
    token = await challenge(client, on.account)
    code = code_now(on.secret, clock)

    signed_in = await finish(client, token, code)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user"]["id"] == on.account.user_id
    assert signed_in.cookies.get("tt_refresh")
    me = await client.get("/v1/me", headers=bearer(signed_in.json()["access_token"]))
    assert me.status_code == 200

    replay = await finish(client, await challenge(client, on.account), code)
    assert (replay.status_code, replay.json()["code"]) == (422, "AUTH_TWO_FACTOR_CODE_INVALID")


async def test_a_recovery_code_signs_in_once(client: AsyncClient, clock: FrozenClock) -> None:
    on = await turn_on(client, clock)
    code = on.recovery_codes[0]
    typed = code.upper().replace("-", " ")  # any case, with a space for the dash

    assert (await finish(client, await challenge(client, on.account), typed)).status_code == 200
    used = await trail(on.account.user_id, "two_factor.recovery_code_used")
    assert [entry.details for entry in used] == [{"left": 9}]
    status = (await client.get(TOTP, headers=on.account.headers)).json()
    assert status["recovery_codes_left"] == 9

    again = await finish(client, await challenge(client, on.account), code)
    assert again.status_code == 422


async def test_wrong_codes_lock_the_account_like_wrong_passwords(mongo_uri: str) -> None:
    clock = earlier_clock()
    settings = build_settings(mongo_uri, auth=auth_settings(lockout_threshold=3))
    async with running_client(build_app(settings, clock)) as client:
        on = await turn_on(client, clock)
        token = await challenge(client, on.account)
        for guess in ("000000", "111111", "abcde-fghjk"):
            assert (await finish(client, token, guess)).status_code == 422

        locked = await finish(client, token, code_now(on.secret, clock))
        assert (locked.status_code, locked.json()["code"]) == (429, "AUTH_ACCOUNT_LOCKED")
        assert (await login(client, on.account.email)).status_code == 429

        clock.advance(seconds=61)
        assert (await finish(client, token, code_now(on.secret, clock))).status_code == 200
        assert (await stored(on.account.user_id)).failed_logins == 0


async def test_a_challenge_is_only_a_challenge(client: AsyncClient, clock: FrozenClock) -> None:
    on = await turn_on(client, clock)
    token = await challenge(client, on.account)

    as_bearer = await client.get("/v1/me", headers=bearer(token))
    assert (as_bearer.status_code, as_bearer.json()["code"]) == (401, "AUTH_TOKEN_INVALID")
    access = await finish(client, on.account.access_token, code_now(on.secret, clock))
    assert (access.status_code, access.json()["code"]) == (
        401,
        "AUTH_TWO_FACTOR_CHALLENGE_INVALID",
    )


async def test_a_challenge_expires(mongo_uri: str) -> None:
    clock = earlier_clock(10)  # the challenge was issued minutes before its five ran out
    async with running_client(build_app(build_settings(mongo_uri), clock)) as client:
        on = await turn_on(client, clock)
        late = await finish(client, await challenge(client, on.account), code_now(on.secret, clock))
        assert (late.status_code, late.json()["code"]) == (401, "AUTH_TWO_FACTOR_CHALLENGE_INVALID")


async def test_a_password_change_ends_a_pending_challenge(
    client: AsyncClient, clock: FrozenClock
) -> None:
    on = await turn_on(client, clock)
    token = await challenge(client, on.account)
    changed = await client.post(
        "/v1/auth/password/change",
        headers=on.account.headers,
        json={"current_password": PASSWORD, "new_password": "Another-Horse-77"},
    )
    assert changed.status_code == 200

    stale = await finish(client, token, code_now(on.secret, clock))
    assert stale.json()["code"] == "AUTH_TWO_FACTOR_CHALLENGE_INVALID"


async def test_a_disabled_account_cannot_finish(client: AsyncClient, clock: FrozenClock) -> None:
    on = await turn_on(client, clock)
    token = await challenge(client, on.account)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(on.account.user_id)}, {"$set": {"disabled_at": clock.now()}}
    )
    refused = await finish(client, token, code_now(on.secret, clock))
    assert (refused.status_code, refused.json()["code"]) == (403, "AUTH_ACCOUNT_DISABLED")


# ---------------------------------------------------------------------- turning it off, renewing


async def test_turning_it_off_takes_the_password_and_a_code(
    client: AsyncClient, clock: FrozenClock
) -> None:
    on = await turn_on(client, clock)
    headers = on.account.headers

    async def disable(**body: Any) -> Response:
        return await client.post(f"{TOTP}/disable", headers=headers, json=body)

    no_password = await disable(code=code_now(on.secret, clock))
    assert no_password.json()["code"] == "AUTH_CURRENT_PASSWORD_INVALID"
    wrong_code = await disable(password=PASSWORD, code="000000")
    assert wrong_code.json()["code"] == "AUTH_TWO_FACTOR_CODE_INVALID"

    assert (await disable(password=PASSWORD, code=code_now(on.secret, clock))).status_code == 204
    status = (await client.get(TOTP, headers=headers)).json()
    assert (status["enabled"], status["recovery_codes_left"]) == (False, 0)
    user = await stored(on.account.user_id)
    assert (user.totp_secret, user.recovery_codes, user.totp_last_step) == (None, [], None)
    assert "access_token" in (await login(client, on.account.email)).json()
    off = await trail(on.account.user_id, "two_factor.disabled")
    assert [entry.details for entry in off] == [{"with": "totp"}]

    again = await disable(password=PASSWORD, code="000000")
    assert (again.status_code, again.json()["code"]) == (409, "AUTH_TWO_FACTOR_OFF")


async def test_new_recovery_codes_replace_the_old(client: AsyncClient, clock: FrozenClock) -> None:
    on = await turn_on(client, clock)
    renewed = await client.post(
        f"{TOTP}/recovery-codes",
        headers=on.account.headers,
        json={"code": code_now(on.secret, clock)},
    )
    assert renewed.status_code == 200
    fresh = renewed.json()["recovery_codes"]
    assert len(fresh) == 10
    assert not set(fresh) & set(on.recovery_codes)
    assert len(await trail(on.account.user_id, "two_factor.recovery_codes_renewed")) == 1

    old = await finish(client, await challenge(client, on.account), on.recovery_codes[0])
    assert old.status_code == 422
    new = await finish(client, await challenge(client, on.account), fresh[0])
    assert new.status_code == 200


async def test_an_admin_reset_turns_it_off(client: AsyncClient, clock: FrozenClock) -> None:
    on = await turn_on(client, clock)
    admin = await admin_account(client)
    reset = await client.post(
        f"/v1/admin/users/{on.account.user_id}/reset-password", headers=admin.headers
    )
    assert reset.status_code == 200
    assert not (await stored(on.account.user_id)).two_factor
    entry = (await trail(on.account.user_id, "user.password_reset"))[0]
    assert entry.details == {"two_factor_cleared": True}

    signed_in = await login(client, on.account.email, reset.json()["one_time_password"])
    assert "access_token" in signed_in.json()  # straight in: the password must change next


# ---------------------------------------------------------------------- without the key


async def test_without_a_key_it_cannot_be_set_up_but_recovery_codes_still_work(
    mongo_uri: str,
) -> None:
    clock = earlier_clock()
    async with running_client(build_app(build_settings(mongo_uri), clock)) as client:
        on = await turn_on(client, clock)

    keyless = build_settings(mongo_uri, auth=auth_settings(field_encryption_key=None))
    async with running_client(build_app(keyless, clock)) as client:
        other = await register(client)
        assert (await client.get(TOTP, headers=other.headers)).json()["available"] is False
        setup = await client.post(f"{TOTP}/setup", headers=other.headers)
        assert (setup.status_code, setup.json()["code"]) == (503, "AUTH_TWO_FACTOR_UNAVAILABLE")

        token = await challenge(client, on.account)
        totp = await finish(client, token, code_now(on.secret, clock))
        assert totp.json()["code"] == "AUTH_TWO_FACTOR_UNAVAILABLE"
        assert (await finish(client, token, on.recovery_codes[0])).status_code == 200


async def test_a_changed_key_cannot_read_secrets_but_recovery_codes_still_work(
    mongo_uri: str,
) -> None:
    clock = earlier_clock()
    async with running_client(build_app(build_settings(mongo_uri), clock)) as client:
        on = await turn_on(client, clock)

    other_key = SecretStr(Fernet.generate_key().decode())
    rekeyed = build_settings(mongo_uri, auth=auth_settings(field_encryption_key=other_key))
    async with running_client(build_app(rekeyed, clock)) as client:
        token = await challenge(client, on.account)
        totp = await finish(client, token, code_now(on.secret, clock))
        assert (totp.status_code, totp.json()["code"]) == (503, "AUTH_TWO_FACTOR_UNAVAILABLE")
        assert (await stored(on.account.user_id)).failed_logins == 0  # the server's fault
        assert (await finish(client, token, on.recovery_codes[0])).status_code == 200


# ---------------------------------------------------------------------- odd input, races


async def test_digits_from_other_scripts_are_a_wrong_code_not_a_crash(
    client: AsyncClient, clock: FrozenClock
) -> None:
    on = await turn_on(client, clock)
    for typed in ("१२३४५६", "௧௨௩௪௫௬"):  # Devanagari, Tamil
        refused = await finish(client, await challenge(client, on.account), typed)
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "AUTH_TWO_FACTOR_CODE_INVALID",
        )

    account = await register(client)
    await client.post(f"{TOTP}/setup", headers=account.headers)
    enable = await client.post(
        f"{TOTP}/enable", headers=account.headers, json={"password": PASSWORD, "code": "१२३४५६"}
    )
    assert (enable.status_code, enable.json()["code"]) == (422, "AUTH_TWO_FACTOR_CODE_INVALID")


@pytest.mark.parametrize("kind", ["totp", "recovery"])
async def test_one_code_sent_twice_at_once_signs_in_once(
    client: AsyncClient, clock: FrozenClock, kind: str
) -> None:
    on = await turn_on(client, clock)
    code = code_now(on.secret, clock) if kind == "totp" else on.recovery_codes[0]
    first, second = await challenge(client, on.account), await challenge(client, on.account)

    results = await asyncio.gather(finish(client, first, code), finish(client, second, code))

    assert sorted(result.status_code for result in results) == [200, 422]
