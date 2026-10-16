"""The ``Idempotency-Key`` request header (ADR-0010): a client sends the same key when it
retries or replays a create, and gets the record the first request made instead of a second
one. Storage is the record itself (``RevisionedRepository.create_once``)."""

from __future__ import annotations

from typing import Annotated, TypeVar

from fastapi import Header, Response

IDEMPOTENCY_HEADER = "Idempotency-Key"
# Set on the response when the record already existed.
REPLAYED_HEADER = "Idempotent-Replayed"

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_HEADER,
        min_length=8,
        max_length=128,
        pattern=r"^[\w.:-]+$",
        description="A unique value per new record (e.g. a UUID); repeat it when retrying.",
    ),
]

T = TypeVar("T")


def answer_once(response: Response, outcome: tuple[T, bool]) -> T:
    """What a once-per-key write answers, from ``(what it made, made now)``. A replay is marked,
    so the client knows it got the first request's answer, not what it sent this time."""
    found, made = outcome
    if not made:
        response.headers[REPLAYED_HEADER] = "true"
    return found
