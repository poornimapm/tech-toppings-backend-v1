"""Find and validate feature modules at startup. Any mistake stops the app with a clear message:
a half-loaded module is worse than none."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from beanie import Document
from fastapi import APIRouter

from app.platform.modules.manifest import ModuleManifest, TileStatProvider

MANIFEST_ATTRIBUTE = "MANIFEST"


class ModuleDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedModule:
    manifest: ModuleManifest
    router: APIRouter | None
    admin_router: APIRouter | None
    documents: tuple[type[Document], ...]
    tile_stat: TileStatProvider | None


def _resolve(path: str, owner: str) -> object:
    module_name, attribute = path.split(":")
    if module_name != owner and not module_name.startswith(f"{owner}."):
        raise ModuleDiscoveryError(f"{path!r} points outside the module package {owner!r}")
    try:
        return getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise ModuleDiscoveryError(f"cannot import {path!r}: {exc}") from exc


def _router(path: str | None, owner: str) -> APIRouter | None:
    if path is None:
        return None
    router = _resolve(path, owner)
    if not isinstance(router, APIRouter):
        raise ModuleDiscoveryError(f"{path!r} is not an APIRouter")
    return router


def _documents(paths: Sequence[str], owner: str) -> tuple[type[Document], ...]:
    documents: list[type[Document]] = []
    for path in paths:
        model = _resolve(path, owner)
        if not (inspect.isclass(model) and issubclass(model, Document)):
            raise ModuleDiscoveryError(f"{path!r} is not a Beanie Document")
        documents.append(model)
    return tuple(documents)


def _tile_stat(path: str | None, owner: str) -> TileStatProvider | None:
    if path is None:
        return None
    provider = _resolve(path, owner)
    if not inspect.iscoroutinefunction(provider):
        raise ModuleDiscoveryError(f"{path!r} must be an async function")
    return cast(TileStatProvider, provider)


def _load_manifest(package_name: str, folder: str) -> ModuleManifest:
    owner = f"{package_name}.{folder}"
    try:
        source = importlib.import_module(f"{owner}.manifest")
    except ModuleNotFoundError as exc:
        if exc.name != f"{owner}.manifest":  # the manifest exists but imports something missing
            raise ModuleDiscoveryError(f"{owner}.manifest cannot be imported: {exc}") from exc
        raise ModuleDiscoveryError(f"module folder {owner!r} has no manifest.py") from exc
    manifest = getattr(source, MANIFEST_ATTRIBUTE, None)
    if not isinstance(manifest, ModuleManifest):
        raise ModuleDiscoveryError(f"{owner}.manifest must define MANIFEST = ModuleManifest(...)")
    if manifest.key != folder:
        raise ModuleDiscoveryError(f"{owner}: manifest key {manifest.key!r} must equal {folder!r}")
    settings = manifest.settings_model
    if settings is not None and settings.model_config.get("extra") != "forbid":
        raise ModuleDiscoveryError(f"{owner}: settings_model must forbid unknown fields")
    return manifest


def discover_modules(packages: Sequence[str]) -> list[LoadedModule]:
    """Load every module under ``packages``, ordered by manifest ``order`` then key."""
    loaded: dict[str, LoadedModule] = {}
    for package_name in packages:
        package = importlib.import_module(package_name)
        folders = sorted(info.name for info in pkgutil.iter_modules(package.__path__) if info.ispkg)
        for folder in folders:
            manifest = _load_manifest(package_name, folder)
            if manifest.key in loaded:
                raise ModuleDiscoveryError(f"duplicate module key {manifest.key!r}")
            owner = f"{package_name}.{folder}"
            loaded[manifest.key] = LoadedModule(
                manifest=manifest,
                router=_router(manifest.router, owner),
                admin_router=_router(manifest.admin_router, owner),
                documents=_documents(manifest.documents, owner),
                tile_stat=_tile_stat(manifest.tile_stat, owner),
            )
    return sorted(loaded.values(), key=lambda module: (module.manifest.order, module.manifest.key))
