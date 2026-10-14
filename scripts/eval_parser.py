"""Accuracy of the expenses rule parser on the labelled inputs, by language and field.

Usage: ``poe eval-parser`` (add ``--misses`` to list what was read wrong).

The same scores gate CI (tests/modules/expenses/test_parser.py). To measure an AI provider
instead, run the app with AGENT_PROVIDERS set and ask the assistant the same inputs: this
script only measures the rules, which need no network and no key.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import yaml

from app.modules.expenses import parsing
from app.modules.expenses.features.categories.service import system_categories
from app.modules.expenses.parsing.scoring import report, score

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "modules" / "expenses" / "parser_cases.yaml"
REFERENCES = ROOT / "app" / "modules" / "expenses" / "skills" / "track-expenses" / "references"
TODAY = date(2026, 10, 8)  # the Thursday the cases are written for


def main() -> int:
    categories: dict[str, set[str]] = {}
    for category in system_categories():
        categories.setdefault(category.kind, set()).add(category.id)
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    scores, misses = score(cases, parsing.load(REFERENCES), TODAY, categories)
    print(f"{len(cases)} inputs, today = {TODAY}\n")
    print(report(scores))
    if "--misses" in sys.argv:
        print("\n" + "\n".join(misses or ["no misses"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
