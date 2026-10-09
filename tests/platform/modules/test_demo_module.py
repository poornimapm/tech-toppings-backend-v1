"""Phase 3 acceptance: dropping a module folder in is all it takes. No edit to the app factory,
the router list or the database setup; the new module is listed, mounted and gated."""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import ModulesSettings
from tests.helpers import TEST_MODULE_PACKAGES, build_app, build_settings, register, running_client

MakePackage = Callable[[dict[str, dict[str, str]]], str]

DEMO_MODULE = {
    "manifest.py": """
from pydantic import BaseModel, ConfigDict

from app.platform import ModuleManifest, ModuleStatus


class DemoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    greeting: str = "hello"


MANIFEST = ModuleManifest(
    key="demo",
    version="0.1.0",
    status=ModuleStatus.BETA,
    order=1,
    settings_model=DemoSettings,
    router="PKG.api:router",
    documents=("PKG.api:Ping",),
    tile_stat="PKG.api:ping_count",
)
""",
    "api.py": """
from typing import ClassVar

from fastapi import APIRouter

from app.platform import (
    ClockDep, OwnedDocument, Scope, ScopedRepository, TileStat, UserScope,
)


class Ping(OwnedDocument):
    MODULE_KEY: ClassVar[str] = "demo"

    class Settings:
        name = "demo_pings"


class PingRepository(ScopedRepository[Ping]):
    model: ClassVar[type[OwnedDocument]] = Ping


router = APIRouter()


@router.post("/pings", status_code=201)
async def ping(scope: UserScope, clock: ClockDep) -> dict[str, str]:
    created = await PingRepository(scope, clock).create({})
    return {"id": str(created.id)}


async def ping_count(scope: Scope, clock: object) -> TileStat:
    count = await Ping.find(PingRepository(scope, clock).query()).count()
    return TileStat(value=count, label="stat_pings")
""",
}


async def test_a_new_module_folder_is_listed_mounted_and_counted(
    module_package: MakePackage, mongo_uri: str
) -> None:
    package = module_package({"demo": DEMO_MODULE})
    settings = build_settings(
        mongo_uri,
        modules=ModulesSettings(_env_file=None, packages=(*TEST_MODULE_PACKAGES, package)),
    )

    async with running_client(build_app(settings)) as client:
        account = await register(client)
        created = await client.post("/v1/demo/pings", headers=account.headers)
        listing = await client.get("/v1/modules?page_size=100", headers=account.headers)
        settings_body = await client.get("/v1/modules/demo/settings", headers=account.headers)

    assert created.status_code == 201
    demo = next(item for item in listing.json()["items"] if item["key"] == "demo")
    assert demo["status"] == "beta"
    assert demo["available"] is True
    assert demo["has_settings"] is True
    assert demo["stat"] == {"value": 1, "format": "number", "currency": None, "label": "stat_pings"}
    assert settings_body.json() == {"greeting": "hello"}


async def test_a_new_modules_routes_require_sign_in(
    module_package: MakePackage, mongo_uri: str
) -> None:
    package = module_package({"demo": DEMO_MODULE})
    settings = build_settings(
        mongo_uri, modules=ModulesSettings(_env_file=None, packages=(package,))
    )

    async with running_client(build_app(settings)) as client:
        response = await client.post("/v1/demo/pings")

    assert response.status_code == 401
