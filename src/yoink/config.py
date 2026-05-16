from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogFormat = Literal["json", "console"]


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

    allowlist_mode: bool = False
    admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    cache_db: Path = Path("/data/yoink.sqlite")
    workdir: Path = Path("/tmp/yoink")
    ig_cookies_file: Path | None = None

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v: object) -> frozenset[int]:
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
        raise TypeError(f"unsupported type for admin_ids: {type(v).__name__}")

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        u = v.upper()
        if u not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {v}")
        return u
