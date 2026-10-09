from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.platform.modules.manifest import ModuleManifest, ModuleStatus, TileStat


def manifest(**overrides: object) -> ModuleManifest:
    values: dict[str, object] = {
        "key": "demo_mod",
        "version": "1.0.0",
        "status": "live",
        "order": 1,
    }
    return ModuleManifest.model_validate(values | overrides)


def test_slug_turns_underscores_into_hyphens() -> None:
    assert manifest(key="gov_docs").slug == "gov-docs"


@pytest.mark.parametrize("key", ["Demo", "1demo", "d", "demo-mod", "x" * 40, ""])
def test_rejects_keys_that_are_not_folder_style(key: str) -> None:
    with pytest.raises(ValidationError):
        manifest(key=key)


@pytest.mark.parametrize("version", ["1", "1.0", "v1.0.0", "1.0.0-beta"])
def test_requires_a_plain_semver(version: str) -> None:
    with pytest.raises(ValidationError):
        manifest(version=version)


@pytest.mark.parametrize("path", ["no_colon", "pkg.mod:", ":attr", "pkg mod:attr"])
def test_code_references_must_be_import_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        manifest(router=path)


def test_coming_soon_modules_cannot_expose_routes() -> None:
    with pytest.raises(ValidationError, match="coming-soon"):
        manifest(status=ModuleStatus.COMING_SOON, router="pkg.demo.router:router")


def test_is_immutable_and_rejects_unknown_fields() -> None:
    item = manifest()
    with pytest.raises(ValidationError):
        item.order = 3  # type: ignore[misc]
    with pytest.raises(ValidationError):
        manifest(colour="red")


def test_lists_every_code_path() -> None:
    class Prefs(BaseModel):
        pass

    item = manifest(
        router="pkg.demo_mod.api:router",
        tile_stat="pkg.demo_mod.stats:count",
        documents=("pkg.demo_mod.models:Thing",),
        settings_model=Prefs,
    )

    assert item.code_paths() == [
        "pkg.demo_mod.api:router",
        "pkg.demo_mod.stats:count",
        "pkg.demo_mod.models:Thing",
    ]


def test_tile_stat_currency_is_an_iso_code() -> None:
    assert TileStat(value=12.5, format="currency", currency="INR", label="stat").currency == "INR"
    with pytest.raises(ValidationError):
        TileStat(value=1, currency="rupees", label="stat")
