"""Photos a run reads (a receipt): held in memory for that run only, never stored or logged.

A photo goes only to providers that read images (``vision``), as an OpenAI-style image part.
Its type is taken from its first bytes, so nothing but an image is ever passed on.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from app.core.config import PhotoType

# Each format's signature: (offset, bytes).
SIGNATURES: dict[PhotoType, tuple[tuple[int, bytes], ...]] = {
    "image/jpeg": ((0, b"\xff\xd8\xff"),),
    "image/png": ((0, b"\x89PNG\r\n\x1a\n"),),
    "image/webp": ((0, b"RIFF"), (8, b"WEBP")),
}


def sniff(data: bytes) -> PhotoType | None:
    """The photo's format from its bytes, or None when it is none of the known ones."""
    return next(
        (
            kind
            for kind, marks in SIGNATURES.items()
            if all(data[at : at + len(mark)] == mark for at, mark in marks)
        ),
        None,
    )


@dataclass(frozen=True)
class Photo:
    data: bytes
    media_type: PhotoType

    def __repr__(self) -> str:  # never the bytes, should a photo reach a log or a traceback
        return f"Photo({self.media_type}, {len(self.data)} bytes)"

    def part(self) -> dict[str, object]:
        encoded = base64.b64encode(self.data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.media_type};base64,{encoded}"},
        }
