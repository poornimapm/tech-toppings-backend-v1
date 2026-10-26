"""``/v1/push``: whether this server can push, and the browsers of mine that receive pushes."""

from __future__ import annotations

from typing import Annotated, ClassVar
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.core.errors import AppError, error_responses
from app.core.rate_limit import rate_limit
from app.platform.auth.deps import CurrentClaims
from app.platform.context import ActorDep
from app.platform.push.service import Push
from app.platform.push.webpush import CURVE, b64decode

router = APIRouter(prefix="/push", tags=["push"])
_B64URL = r"^[A-Za-z0-9_-]+$"
AUTH_SECRET_BYTES = 16
HTTPS_PORT = 443


class PushEndpointError(AppError):
    status_code: ClassVar[int] = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "PUSH_ENDPOINT_NOT_ALLOWED"
    default_message = "That isn't a push service this server sends to."


class PushKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=80, max_length=100, pattern=_B64URL)
    auth: str = Field(min_length=16, max_length=32, pattern=_B64URL)

    @field_validator("p256dh", "auth")
    @classmethod
    def a_real_key(cls, value: str, info: ValidationInfo) -> str:
        raw = b64decode(value)
        if info.field_name == "auth":
            if len(raw) != AUTH_SECRET_BYTES:
                raise ValueError("not a Web Push auth secret")
        else:
            ec.EllipticCurvePublicKey.from_encoded_point(CURVE, raw)  # ValueError if not a point
        return value


class SubscriptionIn(BaseModel):
    """As the browser's `PushSubscription.toJSON()` gives it (``expirationTime`` is ignored)."""

    model_config = ConfigDict(extra="ignore")

    # Printable ASCII, as push services make them: no spaces or control characters.
    endpoint: str = Field(min_length=12, max_length=2048, pattern=r"^https://[!-~]+$")
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def a_plain_url(cls, value: str) -> str:
        """A host and a path: no user name, password or port other than 443, and a URL the
        server can send to. Its host is checked against PUSH_ALLOWED_HOSTS by the route."""
        try:
            parts, _ = urlsplit(value), httpx.URL(value)
            port = parts.port
        except (ValueError, httpx.InvalidURL) as exc:
            raise ValueError("not a push service URL") from exc
        if not parts.hostname or "@" in parts.netloc or port not in (None, HTTPS_PORT):
            raise ValueError("not a push service URL")
        return value


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=12, max_length=2048)


class PushOut(BaseModel):
    available: bool  # the server has VAPID keys and a contact (PUSH_*)
    public_key: str | None  # what a browser subscribes with (applicationServerKey)
    devices: int  # my browsers that receive pushes


class SentOut(BaseModel):
    sent: int


def push_of(ctx: ActorDep) -> Push:
    return Push(ctx)


PushDep = Annotated[Push, Depends(push_of)]


async def _state(push: Push) -> PushOut:
    return PushOut(
        available=push.available,
        public_key=push.settings.vapid_public_key if push.available else None,
        devices=await push.count(),
    )


def _allowed(endpoint: str, hosts: tuple[str, ...]) -> bool:
    host = urlsplit(endpoint).hostname or ""  # lower case; SubscriptionIn parsed it already
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in hosts)


@router.get("", summary="Whether pushes can be sent, and to how many of my browsers")
async def read_push(push: PushDep) -> PushOut:
    return await _state(push)


@router.post(
    "/subscriptions",
    summary="Receive pushes in this browser",
    dependencies=[Depends(rate_limit("push"))],
    responses=error_responses(401, 422, 429, 503),
)
async def subscribe(body: SubscriptionIn, push: PushDep, claims: CurrentClaims) -> PushOut:
    if not _allowed(body.endpoint, push.settings.allowed_hosts):
        raise PushEndpointError
    # Tied to this signed-in device: signing it out ends its pushes.
    await push.subscribe(body.endpoint, body.keys.p256dh, body.keys.auth, claims.session_id)
    return await _state(push)


@router.delete(
    "/subscriptions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop pushes to this browser",
    responses=error_responses(401, 422),
)
async def unsubscribe(body: EndpointIn, push: PushDep) -> None:
    await push.unsubscribe(body.endpoint)


@router.post(
    "/test",
    summary="Send myself a test push",
    dependencies=[Depends(rate_limit("push"))],
    responses=error_responses(401, 429),
)
async def send_test(push: PushDep) -> SentOut:
    sent = await push.send({"type": "push.test", "params": {}})
    return SentOut(sent=sent.delivered)
