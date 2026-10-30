"""Vocabulary matching for static skill scripts.

A skill's ``references/lexicon.yaml`` lists ``actions``; each has ``words`` in any language and
whatever else its script needs. The first action whose words appear in the request wins, so
more specific actions are listed first.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def western_digits(text: str) -> str:
    """Tamil and Devanagari digits written as 0-9."""
    return "".join(
        str(unicodedata.digit(letter)) if letter.isdigit() and not letter.isascii() else letter
        for letter in text
    )


def mentions(text: str, word: str) -> bool:
    """Is ``word`` in normalised ``text``? English matches whole words; Tamil and Hindi words
    take suffixes, so their stems match."""
    word = normalise(word)
    if word.isascii():
        return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None
    return word in text


def load_lexicon(references: Path) -> dict[str, Any]:
    lexicon: dict[str, Any] = yaml.safe_load((references / "lexicon.yaml").read_text("utf-8"))
    return lexicon


def match_action(text: str, lexicon: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """(action name, its entry) for the first action mentioned in ``text``, already normalised."""
    for name, entry in lexicon["actions"].items():
        if any(mentions(text, word) for word in entry["words"]):
            return name, entry
    return None
