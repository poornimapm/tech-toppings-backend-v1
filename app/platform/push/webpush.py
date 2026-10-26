"""Web Push without a library (ADR-0018): message encryption (RFC 8291, ``aes128gcm`` from
RFC 8188) and the server's VAPID identification (RFC 8292), on ``cryptography`` and PyJWT,
both already installed. Keys travel as unpadded base64url, as browsers give them."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
POINT = serialization.PublicFormat.UncompressedPoint
RAW = serialization.Encoding.X962
CURVE = ec.SECP256R1()


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def private_key_from(raw: str) -> ec.EllipticCurvePrivateKey:
    """A P-256 private key from its 32-byte scalar, as `gen_vapid_keys.py` prints it."""
    return ec.derive_private_key(int.from_bytes(b64decode(raw), "big"), CURVE)


def public_bytes(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> bytes:
    public = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return public.public_bytes(RAW, POINT)


def new_key_pair() -> tuple[str, str]:
    """(public, private) for VAPID, base64url: the public key goes to browsers."""
    key = ec.generate_private_key(CURVE)
    scalar = key.private_numbers().private_value.to_bytes(32, "big")
    return b64encode(public_bytes(key)), b64encode(scalar)


def fingerprint(public_key: str) -> str:
    """A short fingerprint of a VAPID public key: which one a browser subscribed with."""
    return hashlib.sha256(b64decode(public_key)).hexdigest()[:16]


def origin(endpoint: str) -> str:
    """``https://host`` of an endpoint, the VAPID audience: never a user name or a port."""
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.hostname}"


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(
    payload: bytes,
    p256dh: str,
    auth: str,
    *,
    salt: bytes | None = None,
    server_key: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """The request body for one subscription: only that browser can read it. ``salt`` and
    ``server_key`` are fresh for every message; tests pass the RFC's to check against it."""
    salt = salt or os.urandom(16)
    server_key = server_key or ec.generate_private_key(CURVE)
    ua_public = b64decode(p256dh)
    as_public = public_bytes(server_key)
    browser = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, ua_public)
    shared = server_key.exchange(ec.ECDH(), browser)
    # RFC 8291 section 3.4: the auth secret and both public keys go into the key.
    ikm = _hmac(
        _hmac(b64decode(auth), shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01"
    )
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    # One record: the payload, then 0x02 marking it as the last.
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + cipher


def vapid_authorization(
    endpoint: str,
    private_key: ec.EllipticCurvePrivateKey,
    subject: str,
    now: datetime,
    expires_in: timedelta,
) -> str:
    """``vapid t=…, k=…``: the push service sees the message comes from this server."""
    claims = {
        "aud": origin(endpoint),
        "exp": int((now + expires_in).timestamp()),
        "sub": subject,
    }
    token = jwt.encode(claims, private_key, algorithm="ES256", headers={"typ": "JWT"})
    return f"vapid t={token}, k={b64encode(public_bytes(private_key))}"
