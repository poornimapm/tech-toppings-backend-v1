from __future__ import annotations

import re

import jwt
import pytest

from app.core.security import (
    PasswordHasher,
    decode_jwt,
    encode_jwt,
    generate_one_time_password,
    hash_token,
    new_token,
    password_policy_violations,
)
from tests.helpers import auth_settings

SETTINGS = auth_settings()


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("Correct-Horse-42", []),
        ("correcthorse42", []),  # two classes is enough by default
        ("short1A", ["too_short"]),
        ("onlylowercaseletters", ["too_simple"]),
        ("x" * 129 + "A1", ["too_long"]),
        (" leading-space-1A", ["surrounding_whitespace"]),
    ],
)
def test_password_policy(password: str, expected: list[str]) -> None:
    assert password_policy_violations(password, SETTINGS) == expected


def test_stricter_policy_counts_character_classes() -> None:
    strict = auth_settings(password_min_classes=4)

    assert password_policy_violations("Correct-Horse-42", strict) == []
    assert password_policy_violations("CorrectHorse42", strict) == ["too_simple"]


async def test_hasher_verifies_and_rejects() -> None:
    hasher = PasswordHasher(SETTINGS)
    hashed = await hasher.hash("Correct-Horse-42")

    assert hashed.startswith("$argon2id$")
    assert await hasher.verify(hashed, "Correct-Horse-42") is True
    assert await hasher.verify(hashed, "wrong") is False
    assert await hasher.verify("not-a-hash", "anything") is False
    assert await hasher.verify(None, "anything") is False  # unknown account: still does work
    assert hasher.needs_rehash(hashed) is False


def test_one_time_passwords_are_readable_and_random() -> None:
    passwords = {generate_one_time_password() for _ in range(50)}

    assert len(passwords) == 50
    assert all(re.fullmatch(r"[A-Za-z2-9]{4}(-[A-Za-z2-9]{4}){3}", p) for p in passwords)
    assert not any(set("0OIl1") & set(p) for p in passwords)


def test_token_digests_are_stable_and_do_not_reveal_the_token() -> None:
    token = new_token()

    assert hash_token(token) == hash_token(token)
    assert re.fullmatch(r"[0-9a-f]{64}", hash_token(token))
    assert token not in hash_token(token)
    assert new_token() != token


def test_jwt_round_trip_and_audience_check() -> None:
    claims = {"sub": "u", "jti": "j", "iat": 1, "exp": 4102444800, "aud": "web", "iss": "api"}
    token = encode_jwt(claims, "k" * 40)

    assert decode_jwt(token, "k" * 40, audience="web", issuer="api")["sub"] == "u"
    with pytest.raises(jwt.InvalidAudienceError):
        decode_jwt(token, "k" * 40, audience="mobile", issuer="api")
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_jwt(encode_jwt({"sub": "u"}, "k" * 40), "k" * 40, audience="web", issuer="api")
