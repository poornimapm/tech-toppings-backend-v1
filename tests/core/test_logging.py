from __future__ import annotations

import logging

import pytest

from app.core.config import LogFormat, LogSettings
from app.core.logging import REDACTED, Redactor, configure_logging, get_logger
from tests.helpers import json_log_lines


def test_redactor_masks_sensitive_keys_at_any_depth_case_insensitively() -> None:
    redactor = Redactor(["password", "token"])
    event = {
        "event": "login",
        "Password": "hunter2",
        "user": {"name": "asha", "refresh_TOKEN": "abc"},
        "attempts": [{"password": "x"}, {"ok": True}],
        "pair": ({"token": "t"}, "plain"),
    }

    scrubbed = redactor(None, "info", event)

    assert scrubbed == {
        "event": "login",
        "Password": REDACTED,
        "user": {"name": "asha", "refresh_TOKEN": REDACTED},
        "attempts": [{"password": REDACTED}, {"ok": True}],
        "pair": ({"token": REDACTED}, "plain"),
    }


def test_json_logging_redacts_and_adds_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LogSettings(_env_file=None, format=LogFormat.JSON, level="info"))

    get_logger("tests.logging").info(
        "user_signed_in", user_id="u1", password="hunter2", session={"cookie": "c"}
    )

    [event] = json_log_lines(capsys.readouterr().out)
    assert event["event"] == "user_signed_in"
    assert event["user_id"] == "u1"
    assert event["password"] == REDACTED
    assert event["session"] == {"cookie": REDACTED}
    assert event["level"] == "info"
    assert event["logger"] == "tests.logging"
    assert event["timestamp"].endswith("Z")


def test_stdlib_loggers_share_the_json_format_and_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LogSettings(_env_file=None, format=LogFormat.JSON, level="warning"))

    logging.getLogger("uvicorn.error").warning("server %s", "starting")
    logging.getLogger("uvicorn.error").info("hidden below WARNING")

    events = json_log_lines(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["server starting"]
    assert events[0]["logger"] == "uvicorn.error"


def test_console_format_is_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LogSettings(_env_file=None, format=LogFormat.CONSOLE, level="info"))

    get_logger("tests.console").info("readable_event", token="secret-value")

    output = capsys.readouterr().out
    assert "readable_event" in output
    assert "secret-value" not in output
    assert not output.lstrip().startswith("{")
