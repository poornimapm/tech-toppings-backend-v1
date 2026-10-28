"""The deployment files stay in step with the settings (docs/deployment.md, ADR-0019): every
setting is documented, and the Render Blueprint, its secrets filled in, starts a production
server that passes the production checks."""

from __future__ import annotations

import base64
import re
from typing import Any

import pytest
import yaml

from app.core.config import PROJECT_ROOT, Environment, SecuritySettings, Settings, load_settings

ENV_EXAMPLE = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
DOCUMENTED = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE, flags=re.MULTILINE))
SERVICE: dict[str, Any] = yaml.safe_load(
    (PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8")
)["services"][0]
SERVER_VARIABLES = {"HOST", "PORT", "FORWARDED_ALLOW_IPS"}  # read by uvicorn, not the app
# What the runbook has the owner make for each secret (docs/deployment.md), in the same shape.
SECRETS = {
    "MONGO_URI": "mongodb+srv://app:secret@cluster0.example.mongodb.net/tech-toppings-v1",
    "AUTH_JWT_SECRET": "g" * 44,  # Render's generateValue: 256 random bits, base64
    "SECURITY_PROXY_SECRET": "p" * 64,
    "JOBS_TOKEN": "j" * 64,
    "AUTH_FIELD_ENCRYPTION_KEY": base64.urlsafe_b64encode(bytes(32)).decode(),
    "AGENT_PROVIDERS": "[]",
}


def setting_names() -> set[str]:
    names: set[str] = set()
    for field in Settings.model_fields.values():
        group = field.annotation
        assert isinstance(group, type)
        prefix = group.model_config.get("env_prefix", "")
        names |= {f"{prefix}{name}".upper() for name in group.model_fields}
    return names


def test_every_setting_is_documented_in_the_env_example() -> None:
    assert setting_names() - DOCUMENTED == set()


def test_the_blueprint_sets_only_real_settings() -> None:
    keys = {item["key"] for item in SERVICE["envVars"]}

    assert keys - setting_names() - SERVER_VARIABLES == set()


def test_the_blueprint_is_one_free_docker_service_checked_without_the_proxy() -> None:
    assert (SERVICE["type"], SERVICE["runtime"], SERVICE["plan"]) == ("web", "docker", "free")
    assert (PROJECT_ROOT / SERVICE["dockerfilePath"]).is_file()
    assert SERVICE["healthCheckPath"] in SecuritySettings(_env_file=None).proxy_exempt_paths


def test_the_blueprint_keeps_every_secret_out_of_git() -> None:
    secret = re.compile(r"(^|_)(URI|SECRET|TOKEN|KEY|PROVIDERS|EMAILS|ID|SUBJECT)$")

    for item in SERVICE["envVars"]:
        if secret.search(item["key"]):
            assert "value" not in item, item["key"]


def test_the_blueprint_with_its_secrets_filled_in_starts_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for item in SERVICE["envVars"]:
        monkeypatch.setenv(item["key"], str(item.get("value", SECRETS.get(item["key"], ""))))

    settings = load_settings([])

    assert settings.app.env is Environment.PRODUCTION
    assert settings.security.proxy_secret is not None
