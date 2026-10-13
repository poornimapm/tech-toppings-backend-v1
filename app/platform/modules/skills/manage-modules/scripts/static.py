"""Static mode for manage-modules: the same tools as the agent, driven by references/lexicon.yaml
instead of a model. Returns None when the request isn't about modules."""

from __future__ import annotations

from difflib import SequenceMatcher

from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
)

MAX_NAME_WORDS = 4


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
    lexicon = load_lexicon(request.references)
    text = normalise(request.input)
    found = match_action(text, lexicon)
    if found is None:
        return None
    action, spec = found

    modules = {item["key"]: item for item in await tools.read("modules_list", {})}
    shown = {item["key"]: item["name"] for item in request.context.get("modules", [])}
    aliases = {
        key: {normalise(shown.get(key, "")), key.replace("_", " ")} - {""} for key in modules
    }
    key = _module_in(text, aliases, float(lexicon["match_threshold"]))
    if key is None:
        # A module verb but no module we recognise: another skill may know better.
        return StaticOutcome("manage-modules.no-module", tentative=True)
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
