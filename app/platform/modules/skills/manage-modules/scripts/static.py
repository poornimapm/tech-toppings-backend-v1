"""Static mode for manage-modules: the same tools as the agent, driven by references/lexicon.yaml
instead of a model. Returns None when the request isn't about modules."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import yaml

from app.platform import StaticOutcome, StaticRequest, ToolBox

MAX_NAME_WORDS = 4


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _mentions(text: str, word: str) -> bool:
    word = _normalise(word)
    if word.isascii():
        return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None
    return word in text  # Tamil/Hindi words take suffixes: match the stem


def _module_in(text: str, aliases: dict[str, set[str]], threshold: float) -> str | None:
    words = text.split()
    windows = {
        " ".join(words[i:j])
        for i in range(len(words))
        for j in range(i + 1, min(len(words), i + MAX_NAME_WORDS) + 1)
    }
    scores = {
        key: max(
            SequenceMatcher(None, alias, window).ratio() for alias in names for window in windows
        )
        for key, names in aliases.items()
    }
    best = max(scores, key=scores.__getitem__, default=None)
    return best if best is not None and scores[best] >= threshold else None


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    lexicon: dict[str, Any] = yaml.safe_load(
        (request.references / "lexicon.yaml").read_text(encoding="utf-8")
    )
    text = _normalise(request.input)
    found = next(
        (
            (name, spec)
            for name, spec in lexicon["actions"].items()
            if any(_mentions(text, word) for word in spec["words"])
        ),
        None,
    )
    if found is None:
        return None
    action, spec = found

    modules = {item["key"]: item for item in await tools.read("modules_list", {})}
    shown = {item["key"]: item["name"] for item in request.context.get("modules", [])}
    aliases = {
        key: {_normalise(shown.get(key, "")), key.replace("_", " ")} - {""} for key in modules
    }
    key = _module_in(text, aliases, float(lexicon["match_threshold"]))
    if key is None:
        return StaticOutcome("manage-modules.no-module")
    params = {"name": shown.get(key, key.replace("_", " "))}

    already = spec.get("already", {})
    if already and all(modules[key].get(field) == value for field, value in already.items()):
        return StaticOutcome(f"manage-modules.already.{action}", params)
    arguments = {"key": key, **spec["arguments"]}
    if tools.tools[spec["tool"]].effect == "client":
        await tools.client(spec["tool"], arguments)
    else:
        tools.propose(
            spec["tool"], arguments, code=f"manage-modules.change.{action}", params=params
        )
    return StaticOutcome(f"manage-modules.ready.{action}", params)
