from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from httpx import AsyncClient

from app.core.config import AppSettings, Environment, get_settings
from app.main import create_app
from tests.helpers import build_settings, running_client

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_openapi.py"


def load_export_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_openapi_documents_the_error_envelope(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert "ErrorResponse" in schema["components"]["schemas"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    readyz_503 = schema["paths"]["/readyz"]["get"]["responses"]["503"]
    assert readyz_503["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")


async def test_docs_can_be_disabled(mongo_uri: str) -> None:
    settings = build_settings(
        mongo_uri, app=AppSettings(_env_file=None, env=Environment.PRODUCTION, docs_enabled=False)
    )
    async with running_client(create_app(settings)) as http:
        docs = await http.get("/docs")
        schema = await http.get("/openapi.json")

    assert docs.status_code == 404
    assert schema.status_code == 404


def test_export_script_writes_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/openapi-export")
    get_settings.cache_clear()
    output = tmp_path / "openapi.json"
    try:
        exit_code = load_export_script().main(["export_openapi.py", str(output)])
    finally:
        get_settings.cache_clear()

    assert exit_code == 0
    schema = json.loads(output.read_text(encoding="utf-8"))
    assert {"/healthz", "/readyz"} <= set(schema["paths"])
