"""Accuracy of receipt scanning on the 20 labelled sample receipts, by language and field.

Usage:
  ``poe eval-receipts``                the rules on the text a phone read from each photo
                                       (tests/modules/expenses/receipts/ocr/, from the frontend's
                                       ``npm run receipt-samples ocr``); add ``--misses``
  ``poe eval-receipts --paper``        the rules on the text as printed: the reader alone
  ``poe eval-receipts --ai``           the AI on the photos (images/), through the providers in
                                       AGENT_PROVIDERS that read images (``vision``), with the
                                       app's prompt, skill, tools and their checks; about three
                                       calls a receipt (``--pause`` seconds apart)

The rules' scores gate CI (tests/modules/expenses/test_receipt_samples.py). The AI's need a key
and the network, so they are run by hand and written into docs/modules/expenses.md.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.clock import SystemClock
from app.core.config import AgentSettings
from app.modules.expenses import parsing
from app.modules.expenses.features.categories.service import system_categories
from app.modules.expenses.parsing.scoring import (
    RECEIPT_FIELDS,
    ReceiptRead,
    report,
    score_receipts,
)
from app.modules.expenses.tools import MAX_ENTRY_DRAFTS
from app.platform.agents.catalog import AgentCatalog, discover_agents
from app.platform.agents.photos import Photo, sniff
from app.platform.agents.providers import ProviderChain
from app.platform.agents.runtime import PLACEHOLDER
from app.platform.plugins import subpackages

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "modules" / "expenses" / "receipts"
SKILLS = ROOT / "app" / "modules" / "expenses" / "skills"
SKILL, AGENT = "scan-receipt", "assistant"


def samples() -> tuple[list[dict[str, Any]], date]:
    data = json.loads((SAMPLES / "cases.json").read_text(encoding="utf-8"))
    return data["cases"], date.fromisoformat(data["today"])


def categories() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for category in system_categories():
        found.setdefault(category.kind, set()).add(category.id)
    return found


def read_by_rules(text: str, lang: str, today: date) -> ReceiptRead:
    lexicon = parsing.load(SKILLS / "track-expenses" / "references")
    words = parsing.load_receipt(SKILLS / SKILL / "references")
    receipt = parsing.read_receipt(
        text,
        today,
        lexicon,
        words,
        locale=lang,
        max_drafts=MAX_ENTRY_DRAFTS,
        categories=categories(),
    )
    first = receipt.drafts[0] if receipt.drafts else None
    return ReceiptRead(
        [(draft.title or "", draft.amount or Decimal(0)) for draft in receipt.drafts],
        first.merchant if first else None,
        first.date if first else None,
        receipt.total,
    )


def ocr_text(case: Mapping[str, Any]) -> str:
    path = SAMPLES / "ocr" / f"{case['id']}.txt"
    if not path.is_file():
        sys.exit(f"{path} is missing: run `npm run receipt-samples ocr` in the frontend first.")
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ the AI, as the app asks it


def _tool_result(
    catalog: AgentCatalog, name: str, arguments: Mapping[str, Any] | None, seen: list[Any]
) -> Any:
    """The skill's tools for a user with no history (the system categories, no habits), with
    the checks the runtime makes: drafts the app would refuse are refused here too."""
    spec = catalog.tools.get(name)
    if spec is None or arguments is None:
        return {"error": "arguments_not_json" if spec else "unknown_tool"}
    try:
        parsed = spec.parse(arguments)
    except ValidationError as exc:
        details = exc.errors(include_url=False, include_context=False, include_input=False)
        return {"error": "invalid_arguments", "details": details}
    if name == "expenses_draft":
        seen.extend(parsed.model_dump()["drafts"])
        return {"status": "shown_to_user"}
    reads = {
        "expenses_categories": [
            {"id": item.id, "kind": item.kind, "name": item.id.replace("_", " ")}
            for item in system_categories()
        ],
        "expenses_habits": [],
    }
    return {"result": reads[name]} if name in reads else {"error": "unknown_tool"}


async def read_by_ai(
    chain: ProviderChain, catalog: AgentCatalog, photo: Photo, lang: str, today: date
) -> ReceiptRead:
    """One photo run as the runtime makes it (agent prompt, the skill preloaded, its tools)."""
    agent, skill = catalog.agents[AGENT], catalog.skills[SKILL]
    context = json.dumps({"modules": [], "input_type": "photo"}, ensure_ascii=False)
    values = {"name": "Eval", "locale": lang, "today": today.isoformat(), "context": context}
    values["skills"] = f"- {skill.name}: {skill.description}"
    system = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), agent.instructions)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "system", "content": skill.instructions},
        {"role": "user", "content": [photo.part()]},
    ]
    tools = [catalog.tools[name].definition() for name in skill.allowed_tools]
    drafts: list[Any] = []
    for _ in range(AgentSettings().max_steps):
        completion = await chain.complete(messages, tools, vision=True, max_tokens=skill.max_tokens)
        messages.append(completion.message)
        if not completion.tool_calls:
            break
        for call in completion.tool_calls:
            result = _tool_result(catalog, call.name, call.arguments, drafts)
            content = json.dumps(result, ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    first = drafts[0] if drafts else {}
    day = first.get("date")  # checked by the tool: YYYY-MM-DD or nothing
    return ReceiptRead(
        [(str(d.get("title") or ""), Decimal(str(d.get("amount") or 0))) for d in drafts],
        first.get("merchant"),
        date.fromisoformat(day) if day else None,
    )


async def all_by_ai(
    cases: list[dict[str, Any]], today: date, pause: float
) -> list[tuple[dict[str, Any], ReceiptRead]]:
    settings = AgentSettings()
    if not any(provider.vision for provider in settings.providers):
        sys.exit("No provider that reads images: set AGENT_PROVIDERS with one marked vision.")
    # The assistant (a platform agent) and the expenses module's skills and tools.
    packages = [package for root in settings.packages for package in subpackages(root)]
    catalog = discover_agents([*packages, "app.modules.expenses"])
    reads = []
    async with httpx.AsyncClient() as http:
        chain = ProviderChain(settings, http, SystemClock())
        for case in cases:
            data = (SAMPLES / "images" / f"{case['id']}.jpg").read_bytes()
            kind = sniff(data)
            if kind is None:
                sys.exit(f"{case['id']}: not a photo")
            reads.append(
                (case, await read_by_ai(chain, catalog, Photo(data, kind), case["lang"], today))
            )
            print(f"read {case['id']}", file=sys.stderr)
            await asyncio.sleep(pause)
    return reads


def main() -> int:
    cases, today = samples()
    args = sys.argv[1:]
    if "--ai" in args:
        pause = float(args[args.index("--pause") + 1]) if "--pause" in args else 5.0
        reads = asyncio.run(all_by_ai(cases, today, pause))
        source = "the AI, on the photos"
    else:
        paper = "--paper" in args
        reads = [
            (
                case,
                read_by_rules(
                    "\n".join(case["paper"]) if paper else ocr_text(case), case["lang"], today
                ),
            )
            for case in cases
        ]
        source = "the rules, on the text as printed" if paper else "the rules, on the phone's OCR"
    scores, misses = score_receipts(reads, with_total="--ai" not in args)
    print(f"{len(cases)} receipts, today = {today}: {source}\n")
    print(report(scores, RECEIPT_FIELDS))
    if "--misses" in args:
        print("\n" + "\n".join(misses or ["no misses"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
