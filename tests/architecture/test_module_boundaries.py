"""Modules are written against ``app.platform`` only (ADR-0001).

import-linter enforces this for the platform sub-packages that exist today; this test also
catches sub-packages added later and covers the test-only modules, which import-linter does
not see.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_DIRS = (ROOT / "app" / "modules", ROOT / "tests" / "fixtures" / "modules")
ALLOWED_APP_IMPORTS = {"app.platform"}


def app_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return [name for name in names if name == "app" or name.startswith("app.")]


def test_modules_import_only_the_platform_api() -> None:
    files = [path for directory in MODULE_DIRS for path in directory.rglob("*.py")]
    assert files, "expected module sources"

    offenders = [
        f"{path.relative_to(ROOT).as_posix()}: {name}"
        for path in files
        for name in app_imports(path)
        if name not in ALLOWED_APP_IMPORTS
    ]

    assert offenders == []


def test_every_module_folder_has_a_manifest() -> None:
    folders = [
        folder
        for directory in MODULE_DIRS
        for folder in directory.iterdir()
        if folder.is_dir() and not folder.name.startswith(("_", "."))
    ]

    assert folders
    assert [f.name for f in folders if not (f / "manifest.py").is_file()] == []
