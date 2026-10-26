"""``Push(ctx)``: the user's browsers that agreed to receive pushes, and sending to them.

Pushing is best effort, on top of the in-app inbox that always has the message (ADR-0012): a
browser whose push service fails, refuses or takes longer than ``PUSH_DEVICE_TIMEOUT_SECONDS``
costs that one try, and one round of pushes never takes longer than ``PUSH_TIMEOUT_SECONDS``,
with at most ``PUSH_MAX_PARALLEL`` of them under way at once. A browser is pushed to only while
the device that subscribed it is signed in, and under the server's current key; it is forgotten
otherwise, or when its push service says it is gone (404/410).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
from beanie import PydanticObjectId
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import status

from app.core.errors import AppError
from app.core.logging import get_logger
from app.platform.auth.models import AuthSession
from app.platform.context import ActorContext
from app.platform.push.models import PushSubscription
from app.platform.push.webpush import encrypt, fingerprint, private_key_from, vapid_authorization

logger = get_logger(__name__)
GONE = {status.HTTP_404_NOT_FOUND, status.HTTP_410_GONE}


class PushUnavailableError(AppError):
    status_code: ClassVar[int] = status.HTTP_503_SERVICE_UNAVAILABLE
    code: ClassVar[str] = "PUSH_UNAVAILABLE"
    default_message = "Notifications on this device aren't available on this server."


@dataclass(frozen=True)
class Sent:
    delivered: int  # pushes a push service took
    done: tuple[bool, ...]  # per message: tried on every browser before the round's deadline

    @property
    def complete(self) -> bool:
        return all(self.done)


Rows = list[list[asyncio.Task[int]]]  # each message's push to each browser, in a round


@dataclass
class _Round:
    slots: asyncio.Semaphore  # pushes under way at once
    failed: set[PydanticObjectId | None] = field(default_factory=set)  # browsers that failed


def _took(task: asyncio.Task[int]) -> int:
    """1 if the push finished and its push service took the message."""
    return 0 if task.cancelled() else task.result()


def _outcome(rows: Rows | None, messages: int) -> Sent:
    """What a round did: a message is done once all of its pushes have finished, whatever each
    push service answered; one still waiting for a slot when the round ended was never tried."""
    if rows is None:  # cut short before any push began
        return Sent(0, done=(False,) * messages)
    delivered = sum(_took(task) for row in rows for task in row)
    return Sent(delivered, done=tuple(not any(task.cancelled() for task in row) for row in rows))


def _reached(browsers: list[PushSubscription], rows: Rows | None) -> list[PydanticObjectId | None]:
    """The browsers whose push service took at least one message of the round."""
    if rows is None:
        return []
    columns = zip(*rows, strict=True)  # each browser's pushes, one per message
    return [
        browser.id
        for browser, pushes in zip(browsers, columns, strict=True)
        if any(map(_took, pushes))
    ]


def _host(endpoint: str) -> str:
    """What a log line names instead of the endpoint, whose path identifies the browser."""
    try:
        return urlsplit(endpoint).hostname or "?"
    except ValueError:
        return "?"


class Push:
    def __init__(self, ctx: ActorContext) -> None:
        if ctx.user.id is None:  # pragma: no cover - persisted users always have an id
            raise AppError
        self.ctx = ctx
        self.settings = ctx.settings.push
        self.user_id: PydanticObjectId = ctx.user.id

    @property
    def available(self) -> bool:
        return self.settings.available

    @cached_property
    def key_id(self) -> str | None:
        public = self.settings.vapid_public_key
        return fingerprint(public) if public is not None else None

    # ------------------------------------------------------------------ subscriptions

    async def subscribe(
        self, endpoint: str, p256dh: str, auth: str, session_id: PydanticObjectId
    ) -> None:
        """Remember this browser for the user, on the signed-in device ``session_id``. A browser
        that was someone else's (a shared computer) starts afresh as the user's. Past
        ``PUSH_MAX_SUBSCRIPTIONS`` the user's oldest others are let go, never this one."""
        if not self.available:
            raise PushUnavailableError
        now, actor = self.ctx.clock.now(), self.user_id
        collection = PushSubscription.get_pymongo_collection()
        await collection.delete_one({"endpoint": endpoint, "user_id": {"$ne": actor}})
        fields = {
            "user_id": actor,
            "module_key": PushSubscription.MODULE_KEY,
            "p256dh": p256dh,
            "auth": auth,
            "session_id": session_id,
            "key_id": self.key_id,
            "updated_at": now,
            "updated_by": actor,
            "deleted_at": None,
        }
        await collection.update_one(
            {"endpoint": endpoint},
            {"$set": fields, "$setOnInsert": {"created_at": now, "created_by": actor}},
            upsert=True,
        )
        others = [item for item in await self._live() if item.endpoint != endpoint]
        await self._forget(others[: max(0, len(others) + 1 - self.settings.max_subscriptions)])

    async def unsubscribe(self, endpoint: str) -> None:
        await PushSubscription.get_pymongo_collection().delete_one(
            {"endpoint": endpoint, "user_id": self.user_id}
        )

    async def count(self) -> int:
        """My browsers that receive pushes (none while this server can't push)."""
        return len(await self._live()) if self.available else 0

    async def has_browsers(self) -> bool:
        """Whether there may be anyone to push to: one cheap read, before any work."""
        if not self.available:
            return False
        return await PushSubscription.find_one({"user_id": self.user_id}) is not None

    async def _mine(self) -> list[PushSubscription]:
        return (
            await PushSubscription.find({"user_id": self.user_id})
            .sort("created_at", "_id")
            .to_list()
        )

    async def _live(self) -> list[PushSubscription]:
        """The user's browsers that may be pushed to: subscribed under the server's current key,
        from a device still signed in. The others are forgotten here, so a device signed out in
        any way (sign-out, revoked, expired, a password change) gets no more pushes."""
        mine = await self._mine()
        current = [item for item in mine if item.key_id == self.key_id]
        signed_in = await self._signed_in({item.session_id for item in current})
        live = [item for item in current if item.session_id in signed_in]
        kept = {item.id for item in live}
        await self._forget([item for item in mine if item.id not in kept])
        return live

    async def _signed_in(self, sessions: set[PydanticObjectId | None]) -> set[Any]:
        """Which of these devices are still signed in: one read."""
        ids = [item for item in sessions if item is not None]
        if not ids:
            return set()
        live = {
            "_id": {"$in": ids},
            "revoked_at": None,
            "expires_at": {"$gt": self.ctx.clock.now()},
        }
        return set(await AuthSession.get_pymongo_collection().distinct("_id", live))

    async def _forget(self, browsers: list[PushSubscription]) -> None:
        if browsers:
            await PushSubscription.get_pymongo_collection().delete_many(
                {"_id": {"$in": [item.id for item in browsers]}}
            )

    # ------------------------------------------------------------------ sending

    async def send(self, *messages: dict[str, Any]) -> Sent:
        """Push each message (JSON) to each of the user's browsers within
        ``PUSH_TIMEOUT_SECONDS``, ``PUSH_MAX_PARALLEL`` pushes at a time, earliest message
        first. Each browser has its own, shorter deadline, from when its push gets a slot, so
        only a long queue or a stall elsewhere (the database) cuts the round short; pushes still
        waiting then are never tried, and ``done`` tells which messages were tried on every
        browser. When each browser last took a push is noted after the round, in one write."""
        if not self.available or not messages:
            return Sent(0, done=(True,) * len(messages))  # nothing to push to
        payloads = [
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
            for message in messages
        ]
        round_ = _Round(asyncio.Semaphore(self.settings.max_parallel))
        browsers: list[PushSubscription] = []
        rows: Rows | None = None
        with suppress(TimeoutError):  # cut short: what had finished still counts
            async with asyncio.timeout(self.settings.timeout_seconds):
                browsers = await self._live()
                async with asyncio.TaskGroup() as group:
                    rows = [
                        [
                            group.create_task(self._send_one(browser, payload, round_))
                            for browser in browsers
                        ]
                        for payload in payloads
                    ]
        sent = _outcome(rows, len(payloads))
        if not sent.complete:
            logger.warning("push_cut_short", left=sent.done.count(False))
        await self._note_sent(_reached(browsers, rows))
        return sent

    async def _send_one(self, browser: PushSubscription, payload: bytes, round_: _Round) -> int:
        """1 if the browser's push service took the message, once one of the round's slots is
        free: no browser's deadline counts that wait. Never raises, so one browser can never
        stop the others (a slow one fails here with a TimeoutError); a log line names its host
        and the error type, nothing more. A browser that failed so is not tried again in the
        round: its other messages count as tried, and hold no slot another browser could use."""
        if browser.id in round_.failed:
            return 0
        async with round_.slots:
            if browser.id in round_.failed:  # failed while this push waited for its slot
                return 0
            try:
                return await self._post(browser, payload)
            except Exception as exc:
                round_.failed.add(browser.id)
                logger.warning(
                    "push_failed", host=_host(browser.endpoint), error_type=type(exc).__name__
                )
                return 0

    async def _note_sent(self, browsers: list[PydanticObjectId | None]) -> None:
        """After the round, so no write holds a push's slot or is cut short with the round. Only
        a note: a failure here never undoes a round the push services took."""
        if not browsers:
            return
        try:
            await PushSubscription.get_pymongo_collection().update_many(
                {"_id": {"$in": browsers}}, {"$set": {"last_sent_at": self.ctx.clock.now()}}
            )
        except Exception as exc:
            logger.warning("push_note_failed", error_type=type(exc).__name__)

    @cached_property
    def _vapid(self) -> tuple[ec.EllipticCurvePrivateKey, str]:
        private, subject = self.settings.vapid_private_key, self.settings.subject
        if private is None or subject is None:  # pragma: no cover - send() checks available
            raise PushUnavailableError
        return private_key_from(private.get_secret_value()), subject

    async def _post(self, browser: PushSubscription, payload: bytes) -> int:
        host = _host(browser.endpoint)
        try:
            body = encrypt(payload, browser.p256dh, browser.auth)
        except ValueError:  # keys no browser could have made: nothing will ever reach it
            await self._forget([browser])
            logger.warning("push_subscription_unusable", host=host)
            return 0
        key, subject = self._vapid
        now = self.ctx.clock.now()
        expires_in = timedelta(hours=self.settings.vapid_expiry_hours)
        headers = {
            "Authorization": vapid_authorization(browser.endpoint, key, subject, now, expires_in),
            "TTL": str(self.settings.ttl_seconds),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Urgency": "normal",
        }
        http: httpx.AsyncClient = self.ctx.state.http
        within = self.settings.device_timeout_seconds  # the whole post, not each phase of it
        async with asyncio.timeout(within):
            response = await http.post(
                browser.endpoint, content=body, headers=headers, timeout=within
            )
        if response.status_code in GONE:
            await self._forget([browser])
            logger.info("push_subscription_gone", host=host)
            return 0
        if not response.is_success:
            logger.warning("push_refused", host=host, status=response.status_code)
            return 0
        return 1
