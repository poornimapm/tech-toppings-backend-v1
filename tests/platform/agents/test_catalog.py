"""Agents, skills and tools come from folders; a malformed one stops startup with its name."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.platform.agents.catalog import discover_agents
from app.platform.plugins import PluginError, subpackages
from tests.helpers import build_app, build_settings

MakePackage = Callable[[dict[str, dict[str, str]]], str]

TOOLS = '''
from app.platform import ActorContext, tool

@tool("read")
async def demo_count(ctx: ActorContext) -> int:
    """Count things."""
    return 1
'''
SKILL = """---
name: {name}
description: Count things for the user.
allowed-tools: {tools}
---
Call demo_count, then say the number.
"""
AGENT = """---
name: helper
description: A test agent.
{skills}---
You help {{name}}. Skills:
{{skills}}
"""


def skill(name: str = "count-things", tools: str = "demo_count") -> str:
    return SKILL.format(name=name, tools=tools)


def agent(skills: str = "") -> str:
    return AGENT.replace("{skills}", skills)


def demo(**files: str) -> dict[str, str]:
    return {
        "tools.py": TOOLS,
        "skills/count-things/SKILL.md": skill(),
        "agents/helper.md": agent(),
        **files,
    }


def catalog_of(plugin_package: MakePackage, *folders: dict[str, str]):  # type: ignore[no-untyped-def]
    package = plugin_package({"demo" if i == 0 else f"demo{i}": f for i, f in enumerate(folders)})
    return discover_agents(subpackages(package))


def test_the_app_ships_the_assistant_and_its_skills(mongo_uri: str) -> None:
    catalog = build_app(build_settings(mongo_uri)).state.agents.catalog

    assert set(catalog.agents["assistant"].skills) == {
        "manage-modules",
        "check-notifications",
        "track-expenses",
        "tidy-products",
        "take-notes",  # from the test module
    }
    assert catalog.skills["manage-modules"].static is not None
    assert (catalog.skills["manage-modules"].owner, catalog.tool_owners["expenses_draft"]) == (
        "modules",
        "expenses",
    )
    assert {name: spec.effect for name, spec in catalog.tools.items()} == {
        "expenses_categories": "read",
        "expenses_habits": "read",
        "expenses_list": "read",
        "expenses_draft": "client",
        "expenses_open": "client",
        "expenses_products": "read",
        "expenses_items": "read",
        "expenses_product_matches": "client",
        "notes_add": "write",
        "modules_list": "read",
        "modules_set_preferences": "write",
        "modules_set_interest": "write",
        "modules_open": "client",
        "notifications_list": "read",
        "notifications_mark_all_read": "write",
        "notifications_open": "client",
    }


def test_a_new_folder_brings_its_agent_skill_and_tools(plugin_package: MakePackage) -> None:
    catalog = catalog_of(plugin_package, demo())

    assert catalog.agents["helper"].skills == ("count-things",)  # no list = every skill
    assert catalog.skills["count-things"].allowed_tools == ("demo_count",)
    assert catalog.skills["count-things"].static is None
    assert "demo_count" in catalog.tools


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"skills/count-things/SKILL.md": "no frontmatter"}, "must start with '---'"),
        ({"skills/count-things/SKILL.md": "---\nname: x\n"}, "not closed"),
        (
            {"skills/count-things/SKILL.md": skill().replace("allowed-tools", "tools")},
            "unknown frontmatter fields",
        ),
        ({"skills/count-things/SKILL.md": skill(name="other-name")}, "must equal its folder"),
        ({"skills/count-things/SKILL.md": skill(tools="demo_missing")}, "unknown tools"),
        ({"skills/count-things/scripts/static.py": "VALUE = 1\n"}, "must define async def run"),
        ({"skills/count-things/scripts/static.py": "import nowhere\n"}, "cannot be imported"),
        ({"agents/helper.md": agent("skills: [nope]\n")}, "unknown skills"),
        ({"agents/helper.md": "---\nname: helper\ndescription: x\n---\n"}, "body"),
        ({"agents/helper.md": agent().replace("name: helper", "name: someone")}, "file name"),
        ({"tools.py": TOOLS.replace("demo_count", "count")}, "must start with 'demo_'"),
        ({"tools.py": TOOLS.replace('"""Count things."""', "")}, "docstring"),
    ],
)
def test_malformed_plugins_stop_startup(
    plugin_package: MakePackage, files: dict[str, str], message: str
) -> None:
    with pytest.raises(PluginError, match=message):
        catalog_of(plugin_package, demo(**files))


def test_skill_names_are_unique_across_packages(plugin_package: MakePackage) -> None:
    second = {"skills/count-things/SKILL.md": skill(tools="")}

    with pytest.raises(PluginError, match="duplicate skill 'count-things'"):
        catalog_of(plugin_package, demo(), second)
