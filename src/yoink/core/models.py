from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MediaKind = Literal["photo", "video", "animation", "document"]


@dataclass(slots=True, kw_only=True)
class MediaItem:
    path: Path
    kind: MediaKind
    width: int | None = None
    height: int | None = None
    duration_s: int | None = None
    mime: str | None = None


@dataclass(slots=True, kw_only=True)
class MediaPackage:
    source_url: str
    provider: str
    items: list[MediaItem]
    caption: str | None = None
    nsfw: bool = False


@dataclass(slots=True, kw_only=True)
class Job:
    chat_id: int
    reply_to_message_id: int | None
    url: str
    user_id: int
    correlation_id: str
