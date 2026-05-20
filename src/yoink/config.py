from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogFormat = Literal["json", "console"]


def _parse_int_set(v: object, *, field: str) -> frozenset[int]:
    if v is None or v == "":
        return frozenset()
    if isinstance(v, frozenset):
        return v
    if isinstance(v, (set, list, tuple)):
        return frozenset(int(x) for x in v)
    if isinstance(v, int):
        return frozenset({v})
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",") if p.strip()]
        return frozenset(int(p) for p in parts)
    raise TypeError(f"unsupported type for {field}: {type(v).__name__}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YOINK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    bot_token: str = Field(min_length=1)
    log_level: str = "INFO"
    log_format: LogFormat = "json"

    workers: int = Field(default=4, ge=1, le=64)
    queue_maxsize: int = Field(default=64, ge=1)
    download_timeout_s: int = Field(default=90, ge=1)
    max_file_mb: int = Field(default=50, ge=1, le=2048)
    rate_per_chat_per_min: int = Field(default=10, ge=1)
    rate_per_user_per_hour: int = Field(default=5, ge=1)

    allowlist_mode: bool = True
    admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    chat_allowlist: Annotated[frozenset[int], NoDecode] = frozenset()

    cache_db: Path = Path("/data/yoink.sqlite")
    workdir: Path = Path("/tmp/yoink")
    ig_cookies_file: Path | None = None

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v: object) -> frozenset[int]:
        return _parse_int_set(v, field="admin_ids")

    @field_validator("chat_allowlist", mode="before")
    @classmethod
    def _parse_chat_allowlist(cls, v: object) -> frozenset[int]:
        return _parse_int_set(v, field="chat_allowlist")

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        u = v.upper()
        if u not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {v}")
        return u
