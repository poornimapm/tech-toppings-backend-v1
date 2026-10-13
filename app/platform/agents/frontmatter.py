"""Markdown with YAML frontmatter, the format of both SKILL.md (Agent Skills) and agent files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.platform.plugins import PluginError

DELIMITER = "---"


@dataclass(frozen=True)
class MarkdownDoc:
    meta: dict[str, Any]
    body: str


def read(path: Path) -> MarkdownDoc:
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    if lines[0].strip() != DELIMITER:
        raise PluginError(f"{path}: must start with '---' frontmatter")
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == DELIMITER), None)
    if end is None:
        raise PluginError(f"{path}: frontmatter is not closed with '---'")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise PluginError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise PluginError(f"{path}: frontmatter must be a mapping")
    return MarkdownDoc(meta=meta, body="\n".join(lines[end + 1 :]).strip())
