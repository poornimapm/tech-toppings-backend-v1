"""Static mode for tidy-products: products and titles that are the same thing, by the word
groups in references/products.yaml and by spelling."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.modules.expenses.parsing.lexicon import singular
from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
)


def _name(text: str) -> str:
    return singular(normalise(text))


@cache
def _groups(references: Path) -> dict[str, int]:
    """Each known name -> the group it belongs to."""
    data = yaml.safe_load((references / "products.yaml").read_text(encoding="utf-8"))
    return {_name(str(word)): index for index, group in enumerate(data) for word in group}


class _Alike:
    def __init__(self, groups: Mapping[str, int], similarity: float) -> None:
        self.groups, self.similarity = groups, similarity

    def names(self, ours: Sequence[str], theirs: Sequence[str]) -> bool:
        return any(self.pair(_name(one), _name(other)) for one in ours for other in theirs)

    def pair(self, one: str, other: str) -> bool:
        group = self.groups.get(one)
        if one == other or (group is not None and group == self.groups.get(other)):
            return True
        return SequenceMatcher(None, one, other).ratio() >= self.similarity


def matches(
    products: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]], alike: _Alike
) -> list[dict[str, Any]]:
    """Each product bought most often keeps the others (and titles) that are the same thing;
    nothing is placed in two matches, and products said to differ stay apart."""
    found: list[dict[str, Any]] = []
    placed: set[str] = set()
    titles: set[str] = set()
    for product in sorted(products, key=lambda item: -item["purchases"]):
        if product["id"] in placed:
            continue
        others = [
            other["id"]
            for other in products
            if other["id"] not in placed | {product["id"]}
            and other["id"] not in product["apart"]
            and product["id"] not in other["apart"]
            and alike.names(product["aliases"], other["aliases"])
        ]
        covered = [
            item["title"]
            for item in items
            if item["title"] not in titles and alike.names(product["aliases"], [item["title"]])
        ]
        if others or covered:
            found.append({"product_id": product["id"], "products": others, "titles": covered})
            placed |= {product["id"], *others}
            titles |= set(covered)
    return found


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    lexicon = load_lexicon(request.references)
    found = match_action(normalise(request.input), lexicon)
    if found is None and not request.chosen:
        return None
    _, action = found or next(iter(lexicon["actions"].items()))
    alike = _Alike(_groups(request.references), float(lexicon["match"]["similarity"]))
    suggested = matches(
        await tools.read("expenses_products", {}), await tools.read("expenses_items", {}), alike
    )
    if not suggested:
        return StaticOutcome("tidy-products.none")
    await tools.client(action["tool"], {"matches": suggested})
    return StaticOutcome("tidy-products.ready.find", {"count": len(suggested)})
