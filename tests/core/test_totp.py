"""TOTP (RFC 6238) and the field cipher that keeps TOTP secrets."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr, ValidationError

from app.core.security import (
    FieldCipher,
    new_recovery_code,
    new_totp_secret,
    totp_code,
    totp_matching_step,
    totp_step,
    totp_uri,
)
from tests.helpers import TEST_FIELD_KEY, auth_settings

# RFC 6238 appendix B: the ASCII key "12345678901234567890", SHA-1, last six of eight digits.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


@pytest.mark.parametrize(
    ("seconds", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_codes_match_the_rfc_vectors(seconds: int, code: str) -> None:
    assert totp_code(RFC_SECRET, totp_step(at(seconds))) == code


def test_a_code_matches_within_the_window_only() -> None:
    now = at(1234567890)
    step = totp_step(now)
    previous = totp_code(RFC_SECRET, step - 1)
    assert totp_matching_step(RFC_SECRET, previous, now, window=1, after=-1) == step - 1
    assert totp_matching_step(RFC_SECRET, previous, now, window=0, after=-1) is None
    two_ago = totp_code(RFC_SECRET, step - 2)
    assert totp_matching_step(RFC_SECRET, two_ago, now, window=1, after=-1) is None


def test_a_used_step_never_matches_again() -> None:
    now = at(1234567890)
    step = totp_step(now)
    code = totp_code(RFC_SECRET, step)
    assert totp_matching_step(RFC_SECRET, code, now, window=1, after=step - 1) == step
    assert totp_matching_step(RFC_SECRET, code, now, window=1, after=step) is None


def test_new_secrets_are_base32_and_unique() -> None:
    secrets = {new_totp_secret() for _ in range(20)}
    assert len(secrets) == 20
    for secret in secrets:
        assert re.fullmatch(r"[A-Z2-7]{32}", secret)
        assert len(totp_code(secret, 1)) == 6


def test_the_uri_is_what_authenticator_apps_read() -> None:
    uri = urlsplit(totp_uri(RFC_SECRET, "asha@example.com", "Tech Toppings"))
    assert (uri.scheme, uri.netloc) == ("otpauth", "totp")
    assert uri.path == "/Tech%20Toppings%3Aasha%40example.com"
    query = parse_qs(uri.query)
    assert query["secret"] == [RFC_SECRET]
    assert query["issuer"] == ["Tech Toppings"]
    assert (query["digits"], query["period"], query["algorithm"]) == (["6"], ["30"], ["SHA1"])


def test_recovery_codes_are_readable_and_unique() -> None:
    codes = {new_recovery_code() for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert re.fullmatch(r"[a-z2-9]{5}-[a-z2-9]{5}", code)
        assert not set(code) & set("01ilo")


def test_the_cipher_round_trips_and_refuses_other_keys() -> None:
    cipher = FieldCipher(SecretStr(TEST_FIELD_KEY))
    sealed = cipher.encrypt(RFC_SECRET)
    assert RFC_SECRET not in sealed
    assert cipher.decrypt(sealed) == RFC_SECRET
    other = FieldCipher(SecretStr(base64.urlsafe_b64encode(b"o" * 32).decode()))
    assert other.decrypt(sealed) is None
    assert cipher.decrypt(sealed[:-4] + "AAAA") is None


@pytest.mark.parametrize("key", ["not base64!", base64.urlsafe_b64encode(b"short").decode()])
def test_settings_refuse_a_key_that_is_not_32_bytes(key: str) -> None:
    with pytest.raises(ValidationError, match="32 bytes"):
        auth_settings(field_encryption_key=key)


def test_a_blank_key_means_two_step_sign_in_is_off() -> None:
    assert auth_settings(field_encryption_key="").field_encryption_key is None
