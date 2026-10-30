"""Discover agents, skills and tools from plugin folders.

Any package scanned (platform sub-packages and every module) may contain:

* ``agents/<name>.md``: an agent (frontmatter ``name``, ``description``, optional ``skills``;
  the body is its instructions, with ``{{placeholders}}`` the runtime fills in);
* ``skills/<name>/SKILL.md``: a skill in the open Agent Skills format, optionally with
  ``scripts/static.py`` (deterministic "static mode") and ``references/`` (its data files);
* ``tools.py``: functions decorated with ``@tool``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.core.errors import NotFoundError
from app.platform.agents import frontmatter
from app.platform.agents.tools import TOOL_ATTRIBUTE, ToolBox, ToolSpec
from app.platform.plugins import PluginError, contributions, load_script, package_dir

NAME_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
# What a skill may be given besides typed or spoken words (its metadata `reads`): text read
# from a photo on the device, or the photo itself.
INPUT_KINDS = frozenset({"ocr", "photo"})
MIN_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS = 16, 32768
SKILL_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
AGENT_FIELDS = {"name", "description", "skills"}
MAX_DESCRIPTION = 1024


@dataclass(frozen=True)
class StaticRequest:
    input: str
    locale: str
    today: date
    context: Mapping[str, Any]
    references: Path  # the skill's references/ folder
    chosen: bool = False  # the caller asked for this skill by name (a page's own button)


@dataclass(frozen=True)
class StaticOutcome:
    """Static mode has no AI to write sentences: it answers with a code the client translates."""

    code: str
    params: Mapping[str, Any] = field(default_factory=dict)
    # "My best answer, but let the other skills try first" (e.g. the right verb, no known object).
    tentative: bool = False


StaticHandler = Callable[[StaticRequest, ToolBox], Awaitable[StaticOutcome | None]]


@dataclass(frozen=True)
class SkillSpec:
    name: str
    owner: str  # the package it came from: a platform area or a module key
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    folder: Path
    static: StaticHandler | None
    reads: frozenset[str] = frozenset()  # INPUT_KINDS it takes (metadata `reads`)
    # The answer it needs room for (metadata `max-output-tokens`): one call drafting a long
    # receipt is far longer than a sentence's answer. None: the provider's own max_tokens.
    max_tokens: int | None = None


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    instructions: str
    skills: tuple[str, ...]


class UnknownAgentError(NotFoundError):
    code = "AGENT_NOT_FOUND"
    default_message = "There is no such agent."


class UnknownSkillError(NotFoundError):
    code = "AGENT_SKILL_NOT_FOUND"
    default_message = "This agent has no such skill."


@dataclass(frozen=True)
class AgentCatalog:
    agents: Mapping[str, AgentSpec]
    skills: Mapping[str, SkillSpec]
    tools: Mapping[str, ToolSpec]
    tool_owners: Mapping[str, str]  # tool name -> the package it came from

    def agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise UnknownAgentError(details={"agent": name})
        return self.agents[name]

    def skills_of(self, agent: AgentSpec, only: str | None = None) -> list[SkillSpec]:
        if only is not None and only not in agent.skills:
            raise UnknownSkillError(details={"skill": only})
        return [self.skills[name] for name in agent.skills if only in (None, name)]

    def tools_of(self, skills: Iterable[SkillSpec]) -> dict[str, ToolSpec]:
        return {name: self.tools[name] for skill in skills for name in skill.allowed_tools}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise PluginError(message)


def _text(meta: Mapping[str, Any], key: str, source: Path) -> str:
    value = meta.get(key)
    _check(isinstance(value, str) and bool(value.strip()), f"{source}: '{key}' is required")
    return str(value).strip()


def _names(value: Any) -> tuple[str, ...]:
    """Agent Skills lists tools space-delimited; YAML lists are accepted too."""
    items = value.split() if isinstance(value, str) else list(value or [])
    return tuple(str(item) for item in items)


def _metadata(meta: Any, source: Path) -> tuple[frozenset[str], int | None]:
    """The skill's `reads` and `max-output-tokens` (Agent Skills metadata: strings)."""
    metadata = meta if isinstance(meta, dict) else {}
    reads = frozenset(str(metadata.get("reads", "")).split())
    _check(reads <= INPUT_KINDS, f"{source}: metadata reads {sorted(reads - INPUT_KINDS)}")
    budget = metadata.get("max-output-tokens")
    if budget is None:
        return reads, None
    _check(str(budget).isdigit(), f"{source}: metadata max-output-tokens must be a number")
    _check(
        MIN_OUTPUT_TOKENS <= int(budget) <= MAX_OUTPUT_TOKENS,
        f"{source}: max-output-tokens out of range",
    )
    return reads, int(budget)


def _load_skill(folder: Path, owner: str) -> SkillSpec:
    source = folder / "SKILL.md"
    doc = frontmatter.read(source)
    unknown = set(doc.meta) - SKILL_FIELDS
    _check(not unknown, f"{source}: unknown frontmatter fields {sorted(unknown)}")
    name, description = _text(doc.meta, "name", source), _text(doc.meta, "description", source)
    _check(name == folder.name, f"{source}: name {name!r} must equal its folder {folder.name!r}")
    _check(re.fullmatch(NAME_PATTERN, name) is not None, f"{source}: invalid name {name!r}")
    _check(len(description) <= MAX_DESCRIPTION, f"{source}: description is too long")
    script = folder / "scripts" / "static.py"
    static = None
    if script.is_file():
        module = load_script(script, f"tt_skill_{owner}_{name.replace('-', '_')}_static")
        static = getattr(module, "run", None)
        _check(callable(static), f"{script}: must define async def run(request, tools)")
    reads, budget = _metadata(doc.meta.get("metadata"), source)
    tools = _names(doc.meta.get("allowed-tools"))
    return SkillSpec(name, owner, description, doc.body, tools, folder, static, reads, budget)


def _load_agent(path: Path) -> AgentSpec:
    doc = frontmatter.read(path)
    unknown = set(doc.meta) - AGENT_FIELDS
    _check(not unknown, f"{path}: unknown frontmatter fields {sorted(unknown)}")
    name = _text(doc.meta, "name", path)
    _check(name == path.stem, f"{path}: name {name!r} must equal the file name")
    _check(bool(doc.body), f"{path}: the body (the agent's instructions) is empty")
    return AgentSpec(
        name, _text(doc.meta, "description", path), doc.body, _names(doc.meta.get("skills"))
    )


def _unique(items: Iterable[Any], kind: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for item in items:
        _check(item.name not in found, f"duplicate {kind} {item.name!r}")
        found[item.name] = item
    return found


def discover_agents(packages: Iterable[str]) -> AgentCatalog:
    agents: list[AgentSpec] = []
    skills: list[SkillSpec] = []
    tools: list[ToolSpec] = []
    owners: dict[str, str] = {}
    for package in packages:
        owner, folder = package.rsplit(".", 1)[-1], package_dir(package)
        found: list[ToolSpec] = contributions(package, "tools", TOOL_ATTRIBUTE)
        tools += found
        owners |= {spec.name: owner for spec in found}
        agents += [_load_agent(path) for path in sorted(folder.glob("agents/*.md"))]
        skills += [_load_skill(md.parent, owner) for md in sorted(folder.glob("skills/*/SKILL.md"))]

    tool_map, skill_map = _unique(tools, "tool"), _unique(skills, "skill")
    for skill in skill_map.values():
        missing = [name for name in skill.allowed_tools if name not in tool_map]
        _check(not missing, f"skill {skill.name!r} allows unknown tools {missing}")
    agent_map: dict[str, AgentSpec] = {}
    for agent in _unique(agents, "agent").values():
        names = agent.skills or tuple(skill_map)  # no list = every skill
        missing = [name for name in names if name not in skill_map]
        _check(not missing, f"agent {agent.name!r} uses unknown skills {missing}")
        agent_map[agent.name] = AgentSpec(agent.name, agent.description, agent.instructions, names)
    return AgentCatalog(agents=agent_map, skills=skill_map, tools=tool_map, tool_owners=owners)
