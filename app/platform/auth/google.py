"""Verifies Google Identity Services ID tokens (the "Sign in with Google" button flow).

Only the public client id is needed: the token's RS256 signature is checked against Google's
published keys (fetched over HTTPS, cached), and so are audience, issuer, expiry and
``email_verified``. No client secret is stored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from app.core.config import AuthSettings
from app.platform.auth.errors import GoogleTokenInvalidError


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    name: str | None


class GoogleIdTokenVerifier:
    def __init__(self, settings: AuthSettings, client_id: str, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client_id = client_id
        self.http = http
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0

    async def _signing_key(self, kid: str) -> PyJWK:
        stale = time.monotonic() - self._fetched_at > self.settings.google_jwks_cache_seconds
        if stale or kid not in self._keys:
            await self._refresh_keys()
        key = self._keys.get(kid)
        if key is None:
            raise GoogleTokenInvalidError
        return key

    async def _refresh_keys(self) -> None:
        try:
            response = await self.http.get(self.settings.google_jwks_url)
            response.raise_for_status()
            jwks: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleTokenInvalidError("Google keys are unavailable right now.") from exc
        self._keys = {
            str(entry["kid"]): PyJWK(entry) for entry in jwks.get("keys", []) if "kid" in entry
        }
        self._fetched_at = time.monotonic()

    async def verify(self, id_token: str) -> GoogleIdentity:
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
            if not isinstance(kid, str):
                raise GoogleTokenInvalidError
            key = await self._signing_key(kid)
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=list(self.settings.google_issuers),
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.InvalidTokenError as exc:
            raise GoogleTokenInvalidError from exc
        email = claims.get("email")
        if not isinstance(email, str) or claims.get("email_verified") is not True:
            raise GoogleTokenInvalidError("The Google account has no verified email.")
        name = claims.get("name")
        return GoogleIdentity(
            subject=str(claims["sub"]),
            email=email,
            name=name if isinstance(name, str) else None,
        )
