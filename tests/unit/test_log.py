from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

from yoink.log import _redact_secrets, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    structlog.reset_defaults()


def _capture_structlog_output() -> tuple[io.StringIO, logging.Handler]:
    """Attach a stream handler to the root logger so structlog output can be captured."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return buf, handler


def _detach_handler(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)


def test_redact_secrets_masks_known_keys() -> None:
    event = {"bot_token": "abc", "Token": "xyz", "cookies": "c", "msg": "hi"}
    result = _redact_secrets(None, "evt", event)
    assert result["bot_token"] == "***"
    assert result["Token"] == "***"
    assert result["cookies"] == "***"
    assert result["msg"] == "hi"


def test_configure_logging_json_format_emits_json() -> None:
    buf, handler = _capture_structlog_output()
    try:
        configure_logging(level="DEBUG", fmt="json")
        get_logger("test").info("hello", extra_field=1, bot_token="secret-leak")
    finally:
        _detach_handler(handler)
    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["level"] == "info"
    assert payload["extra_field"] == 1
    assert payload["bot_token"] == "***"
    assert "timestamp" in payload


def test_configure_logging_console_format_emits_human_readable() -> None:
    buf, handler = _capture_structlog_output()
    try:
        configure_logging(level="INFO", fmt="console")
        get_logger().info("hello console", extra_field="value")
    finally:
        _detach_handler(handler)
    output = buf.getvalue()
    assert "hello console" in output
    assert "extra_field" in output


def test_configure_logging_unknown_level_defaults_to_info() -> None:
    buf, handler = _capture_structlog_output()
    try:
        configure_logging(level="NOPE", fmt="json")
        log = get_logger("x")
        log.debug("should-not-appear")
        log.info("ok")
    finally:
        _detach_handler(handler)
    output = buf.getvalue()
    assert "should-not-appear" not in output
    line = output.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "ok"
    assert payload["level"] == "info"
