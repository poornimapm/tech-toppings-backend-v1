"""Folder-based plugin loading shared by module and agent discovery: "add a folder" is the
only registration step anywhere in the platform."""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any


class PluginError(RuntimeError):
    """A plugin folder is malformed. Startup stops with this message."""


def subpackages(package: str) -> list[str]:
    """Fully qualified names of the package's direct sub-packages, sorted."""
    module = importlib.import_module(package)
    return [
        f"{package}.{info.name}"
        for info in sorted(pkgutil.iter_modules(module.__path__), key=lambda item: item.name)
        if info.ispkg
    ]


def package_dir(package: str) -> Path:
    return Path(next(iter(importlib.import_module(package).__path__)))


def import_optional(name: str) -> ModuleType | None:
    """Import ``name`` if it exists. A module that exists but fails to import is an error."""
    try:
        return importlib.import_module(name)
    except Exception as exc:  # a broken plugin must name itself, whatever went wrong
        if isinstance(exc, ModuleNotFoundError) and exc.name == name:
            return None
        raise PluginError(f"{name} cannot be imported: {exc}") from exc


def contributions(package: str, module: str, attribute: str) -> list[Any]:
    """What ``<package>.<module>`` contributes: the value a decorator stored under ``attribute``
    on each of its functions (``@tool`` in tools.py, ``@widget`` in widgets.py ...). Names
    must start with the package's own key, so the owner is always visible: ``modules_list``.
    """
    source, owner = import_optional(f"{package}.{module}"), package.rsplit(".", 1)[-1]
    found = [getattr(value, attribute, None) for value in vars(source).values()] if source else []
    specs = [spec for spec in found if spec is not None]
    for spec in specs:
        if not spec.name.startswith(f"{owner}_"):
            raise PluginError(f"{package}.{module}: {spec.name} must start with '{owner}_'")
    return specs


def owned_contributions(
    packages: Iterable[str], module: str, attribute: str
) -> list[tuple[str, Any]]:
    """``(owner, spec)`` for what every package contributes; names are unique across them."""
    found = [
        (package.rsplit(".", 1)[-1], spec)
        for package in packages
        for spec in contributions(package, module, attribute)
    ]
    names = [spec.name for _, spec in found]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise PluginError(f"duplicate names in {module}.py: {duplicates}")
    return found


def load_script(path: Path, name: str) -> ModuleType:
    """Import a script by file path (skill folders are data, not packages)."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - only for non-.py paths
        raise PluginError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise PluginError(f"{path} cannot be imported: {exc}") from exc
    return module
