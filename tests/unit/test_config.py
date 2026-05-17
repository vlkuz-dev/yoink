from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from yoink.config import Settings


def test_defaults_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "test-token")

    s = Settings()  # type: ignore[call-arg]

    assert s.bot_token == "test-token"
    assert s.log_level == "INFO"
    assert s.log_format == "json"
    assert s.workers == 4
    assert s.queue_maxsize == 64
    assert s.download_timeout_s == 90
    assert s.max_file_mb == 50
    assert s.rate_per_chat_per_min == 10
    assert s.allowlist_mode is True
    assert s.admin_ids == frozenset()
    assert s.chat_allowlist == frozenset()
    assert s.cache_db == Path("/data/yoink.sqlite")
    assert s.workdir == Path("/tmp/yoink")
    assert s.ig_cookies_file is None


def test_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_admin_ids_parses_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_ADMIN_IDS", "111, 222 ,333")

    s = Settings()  # type: ignore[call-arg]

    assert s.admin_ids == frozenset({111, 222, 333})
    assert isinstance(s.admin_ids, frozenset)


def test_admin_ids_empty_string_yields_empty_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_ADMIN_IDS", "")

    s = Settings()  # type: ignore[call-arg]

    assert s.admin_ids == frozenset()


def test_admin_ids_rejects_non_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_ADMIN_IDS", "111,abc")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_chat_allowlist_parses_signed_ints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_CHAT_ALLOWLIST", "-1001234567890, 123 , -100200300400")

    s = Settings()  # type: ignore[call-arg]

    assert s.chat_allowlist == frozenset({-1001234567890, 123, -100200300400})
    assert isinstance(s.chat_allowlist, frozenset)


def test_chat_allowlist_empty_string_yields_empty_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_CHAT_ALLOWLIST", "")

    s = Settings()  # type: ignore[call-arg]

    assert s.chat_allowlist == frozenset()


def test_chat_allowlist_rejects_non_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_CHAT_ALLOWLIST", "-100,foo")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_log_level_uppercased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_LOG_LEVEL", "debug")

    s = Settings()  # type: ignore[call-arg]

    assert s.log_level == "DEBUG"


def test_log_level_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_LOG_LEVEL", "trace")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_log_format_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_LOG_FORMAT", "xml")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_workers_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_WORKERS", "0")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_paths_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOINK_BOT_TOKEN", "t")
    monkeypatch.setenv("YOINK_CACHE_DB", "/var/lib/yoink/cache.db")
    monkeypatch.setenv("YOINK_WORKDIR", "/scratch/yoink")
    monkeypatch.setenv("YOINK_IG_COOKIES_FILE", "/secrets/ig.cookies")

    s = Settings()  # type: ignore[call-arg]

    assert s.cache_db == Path("/var/lib/yoink/cache.db")
    assert s.workdir == Path("/scratch/yoink")
    assert s.ig_cookies_file == Path("/secrets/ig.cookies")
