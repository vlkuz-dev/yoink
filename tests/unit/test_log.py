from __future__ import annotations

from yoink.log import _redact_secrets, configure_logging, get_logger


def test_redact_secrets_masks_known_keys() -> None:
    event = {"bot_token": "abc", "Token": "xyz", "cookies": "c", "msg": "hi"}
    result = _redact_secrets(None, "evt", event)
    assert result["bot_token"] == "***"
    assert result["Token"] == "***"
    assert result["cookies"] == "***"
    assert result["msg"] == "hi"


def test_configure_logging_json_format() -> None:
    configure_logging(level="DEBUG", fmt="json")
    log = get_logger("test")
    log.info("hello", extra_field=1)


def test_configure_logging_console_format() -> None:
    configure_logging(level="INFO", fmt="console")
    log = get_logger()
    log.info("hello console")


def test_configure_logging_unknown_level_defaults_to_info() -> None:
    configure_logging(level="NOPE", fmt="json")
    log = get_logger("x")
    log.info("ok")
