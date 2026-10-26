"""Web Push encryption and VAPID, checked against RFC 8291's worked example and by reading the
message back the way a browser does."""

from __future__ import annotations

import hashlib
import hmac
import struct
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.platform.push.webpush import (
    CURVE,
    b64decode,
    b64encode,
    encrypt,
    fingerprint,
    new_key_pair,
    origin,
    private_key_from,
    public_bytes,
    vapid_authorization,
)

# RFC 8291, Appendix A.
PLAINTEXT = b"When I grow up, I want to be a watermelon"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
AS_PUBLIC = (
    "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
MESSAGE = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6"
    "TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulc"
    "y4a-fN"
)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """What the browser does with the body (RFC 8291 section 3.4, read side)."""
    salt, (record_size, id_length) = body[:16], struct.unpack("!IB", body[16:21])
    as_public = body[21 : 21 + id_length]
    assert record_size == 4096
    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(CURVE, as_public)
    )
    ua_public = public_bytes(ua_private)
    ikm = _hmac(_hmac(auth, shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, body[21 + id_length :], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_encrypts_exactly_as_rfc_8291_appendix_a() -> None:
    body = encrypt(
        PLAINTEXT, UA_PUBLIC, AUTH, salt=b64decode(SALT), server_key=private_key_from(AS_PRIVATE)
    )
    assert b64encode(public_bytes(private_key_from(AS_PRIVATE))) == AS_PUBLIC
    assert b64encode(body) == MESSAGE


def test_only_the_subscribed_browser_can_read_a_message() -> None:
    browser = ec.generate_private_key(CURVE)
    auth = b"sixteen byte key"
    body = encrypt(b'{"type":"x"}', b64encode(public_bytes(browser)), b64encode(auth))
    assert decrypt(body, browser, auth) == b'{"type":"x"}'
    again = encrypt(b'{"type":"x"}', b64encode(public_bytes(browser)), b64encode(auth))
    assert again != body  # a fresh salt and key every time


def test_vapid_names_the_push_service_and_this_server() -> None:
    public, private = new_key_pair()
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    header = vapid_authorization(
        "https://fcm.googleapis.com/fcm/send/abc:def",
        private_key_from(private),
        "mailto:ops@example.com",
        now,
        timedelta(hours=12),
    )
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert key == public
    verifying = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, b64decode(public))
    claims = jwt.decode(
        token,
        verifying,
        algorithms=["ES256"],
        audience="https://fcm.googleapis.com",
        options={"verify_exp": False},
    )
    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": int((now + timedelta(hours=12)).timestamp()),
        "sub": "mailto:ops@example.com",
    }
    assert jwt.get_unverified_header(token)["typ"] == "JWT"
    assert len(b64decode(UA_PRIVATE)) == 32  # the RFC's keys are raw scalars, like ours


def test_the_audience_is_the_push_services_origin_only() -> None:
    assert origin("https://FCM.googleapis.com:443/fcm/send/abc") == "https://fcm.googleapis.com"
    assert fingerprint(AS_PUBLIC) == fingerprint(AS_PUBLIC) != fingerprint(UA_PUBLIC)
