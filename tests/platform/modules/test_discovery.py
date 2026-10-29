from __future__ import annotations

from collections.abc import Callable

import pytest

from app.platform.modules.discovery import ModuleDiscoveryError, discover_modules
from app.platform.modules.manifest import ModuleStatus

MakePackage = Callable[[dict[str, dict[str, str]]], str]

MANIFEST = """
from app.platform import ModuleManifest
MANIFEST = ModuleManifest(key="{key}", version="1.0.0", status="live", order={order}{extra})
"""


def manifest(key: str, order: int = 1, **extra: str) -> str:
    fields = "".join(f", {name}={value}" for name, value in extra.items())
    return MANIFEST.format(key=key, order=order, extra=fields)


def test_discovers_the_real_modules() -> None:
    modules = discover_modules(("app.modules",))

    manifests = {module.manifest.key: module.manifest for module in modules}
    assert manifests["expenses"].status is ModuleStatus.BETA
    shipped = {"medicine", "receipts", "study", "gov_docs", "meals", "habits", "journal"}
    assert shipped <= set(manifests)  # shipped as upcoming; `poe new-module` may build any
    for manifest in manifests.values():
        # Upcoming: a tile and nothing behind it; built: its own routes.
        assert (manifest.status is ModuleStatus.COMING_SOON) == (manifest.router is None)


def test_orders_by_manifest_order_then_key(plugin_package: MakePackage) -> None:
    package = plugin_package({
        "beta_mod": {"manifest.py": manifest("beta_mod", order=2)},
        "alpha_mod": {"manifest.py": manifest("alpha_mod", order=2)},
        "first_mod": {"manifest.py": manifest("first_mod", order=1)},
    })  # fmt: skip

    keys = [module.manifest.key for module in discover_modules((package,))]

    assert keys == ["first_mod", "alpha_mod", "beta_mod"]


def test_resolves_routers_documents_and_tile_stats(plugin_package: MakePackage) -> None:
    package = plugin_package({
        "demo": {
            "manifest.py": manifest(
                "demo", router='"PKG.api:router"', documents='("PKG.models:Thing",)',
                tile_stat='"PKG.api:stat"',
            ),
            "api.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"
                      "async def stat(scope, clock):\n    return None\n",
            "models.py": "from app.platform import OwnedDocument\n"
                         "class Thing(OwnedDocument):\n    MODULE_KEY = 'demo'\n",
        }
    })  # fmt: skip

    (module,) = discover_modules((package,))

    assert module.router is not None
    assert [document.__name__ for document in module.documents] == ["Thing"]
    assert module.tile_stat is not None


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({}, "has no manifest.py"),
        ({"manifest.py": "import tt_not_installed\n"}, "cannot be imported"),
        ({"manifest.py": "MANIFEST = {'key': 'demo'}\n"}, "must define MANIFEST"),
        ({"manifest.py": manifest("other")}, "must equal 'demo'"),
        (
            {"manifest.py": manifest("demo", router='"os.path:join"')},
            "points outside the module package",
        ),
        ({"manifest.py": manifest("demo", router='"PKG.missing:router"')}, "cannot import"),
        (
            {"manifest.py": manifest("demo", router='"PKG.api:router"'), "api.py": "router = 1\n"},
            "is not an APIRouter",
        ),
        (
            {
                "manifest.py": manifest("demo", documents='("PKG.api:Thing",)'),
                "api.py": "Thing = 1\n",
            },
            "is not a Beanie Document",
        ),
        (
            {
                "manifest.py": manifest("demo", tile_stat='"PKG.api:stat"'),
                "api.py": "def stat(scope, clock):\n    return None\n",
            },
            "must be an async function",
        ),
        (
            {
                "manifest.py": "from pydantic import BaseModel\n"
                "class Prefs(BaseModel):\n    x: int = 1\n"
                + manifest("demo", settings_model="Prefs"),
            },
            "must forbid unknown fields",
        ),
    ],
)
def test_rejects_broken_modules_with_a_clear_message(
    plugin_package: MakePackage, files: dict[str, str], message: str
) -> None:
    package = plugin_package({"demo": files})

    with pytest.raises(ModuleDiscoveryError, match=message):
        discover_modules((package,))


def test_rejects_the_same_key_in_two_packages(plugin_package: MakePackage) -> None:
    first = plugin_package({"demo": {"manifest.py": manifest("demo")}})
    second = plugin_package({"demo": {"manifest.py": manifest("demo")}})

    with pytest.raises(ModuleDiscoveryError, match="duplicate module key 'demo'"):
        discover_modules((first, second))
