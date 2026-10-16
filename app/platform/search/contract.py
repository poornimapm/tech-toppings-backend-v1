"""The search contract.

A search provider is an async function ``(ctx: ActorContext, text: str, limit: int)`` in a
package's ``search.py``, decorated with ``@search``. It returns at most ``limit`` hits among the
caller's own things (read through ``ctx.scope``), the most useful first. ``text`` arrives
trimmed, with its spaces squeezed and long enough to search for; match it as people type: any
case, part of a word.

A hit names where it opens within its module (``path``), never a whole URL, so a module can
only lead into itself.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from app.platform.context import ActorContext

SEARCH_ATTRIBUTE = "__platform_search__"


class SearchHit(BaseModel):
    """One thing found: the user's own words for it, and what helps tell it apart."""

    kind: str = Field(pattern=r"^[a-z_]{1,30}$", description="What it is; the client names it")
    id: str
    title: str
    path: str = Field(
        pattern=r"^[A-Za-z0-9?][A-Za-z0-9_\-./?=&%]*$",
        description="Where it opens within its module, e.g. entries/<id> or ?view=<id>",
    )
    amount_minor: int | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    date: dt.date | None = None


SearchProvider = Callable[[ActorContext, str, int], Awaitable[list[SearchHit]]]
F = TypeVar("F", bound=SearchProvider)


@dataclass(frozen=True)
class SearchSpec:
    name: str
    fn: SearchProvider


def search(fn: F) -> F:
    target: Callable[..., Any] = fn
    if not inspect.iscoroutinefunction(target):
        raise TypeError(f"search provider {fn.__name__} must be async")
    setattr(fn, SEARCH_ATTRIBUTE, SearchSpec(fn.__name__, fn))
    return fn
