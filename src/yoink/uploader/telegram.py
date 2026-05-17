from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from yoink.cache.store import CachedFile
from yoink.core.errors import MediaTooLarge

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from aiogram import Bot

    from yoink.core.models import MediaItem, MediaKind, MediaPackage

    SleepFn = Callable[[float], Awaitable[None]]


_MEDIA_GROUP_MAX = 10
_RETRY_PAD_S = 0.5
_TOO_BIG_PHRASES: tuple[str, ...] = (
    "file is too big",
    "request entity too large",
)
_GROUPABLE: frozenset[str] = frozenset({"photo", "video"})

T = TypeVar("T")


def _is_too_big(exc: TelegramBadRequest) -> bool:
    text = (exc.message or str(exc)).lower()
    return any(phrase in text for phrase in _TOO_BIG_PHRASES)


def _extract_file_id(msg: Message, kind: MediaKind) -> str:
    if kind == "photo":
        if not msg.photo:
            raise RuntimeError("send_photo returned message without photo")
        return msg.photo[-1].file_id
    if kind == "video":
        if msg.video is None:
            raise RuntimeError("send_video returned message without video")
        return msg.video.file_id
    if kind == "animation":
        if msg.animation is None:
            raise RuntimeError("send_animation returned message without animation")
        return msg.animation.file_id
    if msg.document is None:
        raise RuntimeError("send_document returned message without document")
    return msg.document.file_id


def _input_for_groupable(
    item: MediaItem,
    *,
    caption: str | None,
) -> InputMediaPhoto | InputMediaVideo:
    media = FSInputFile(str(item.path))
    if item.kind == "photo":
        return InputMediaPhoto(media=media, caption=caption)
    if item.kind == "video":
        return InputMediaVideo(
            media=media,
            caption=caption,
            width=item.width,
            height=item.height,
            duration=item.duration_s,
            supports_streaming=True,
        )
    raise RuntimeError(f"non-groupable kind reached _input_for_groupable: {item.kind}")


def _chunks(seq: list[T], size: int) -> list[list[T]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _path_for(item: MediaItem | None) -> Path | None:
    return item.path if item is not None else None


class TelegramUploader:
    def __init__(
        self,
        bot: Bot,
        *,
        sleep: SleepFn | None = None,
    ) -> None:
        self._bot = bot
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep

    async def send(
        self,
        chat_id: int,
        reply_to: int | None,
        package: MediaPackage,
    ) -> list[CachedFile]:
        items = package.items
        if not items:
            return []

        groupable: list[tuple[int, MediaItem]] = [
            (i, it) for i, it in enumerate(items) if it.kind in _GROUPABLE
        ]
        singular: list[tuple[int, MediaItem]] = [
            (i, it) for i, it in enumerate(items) if it.kind not in _GROUPABLE
        ]

        out: dict[int, CachedFile] = {}
        caption = package.caption
        caption_used = False

        if len(groupable) >= 2:
            caption_used = await self._send_group(
                chat_id=chat_id,
                reply_to=reply_to,
                groupable=groupable,
                caption=caption,
                out=out,
            )
        elif len(groupable) == 1:
            orig_idx, item = groupable[0]
            effective_caption = caption if not caption_used else None
            msg = await self._send_single(
                chat_id=chat_id,
                reply_to=reply_to,
                item=item,
                caption=effective_caption,
            )
            if effective_caption is not None:
                caption_used = True
            out[orig_idx] = CachedFile(
                file_id=_extract_file_id(msg, item.kind),
                kind=item.kind,
                mime=item.mime,
            )

        singular_reply = reply_to if not groupable else None
        for orig_idx, item in singular:
            effective_caption = caption if not caption_used else None
            msg = await self._send_single(
                chat_id=chat_id,
                reply_to=singular_reply,
                item=item,
                caption=effective_caption,
            )
            if effective_caption is not None:
                caption_used = True
            singular_reply = None
            out[orig_idx] = CachedFile(
                file_id=_extract_file_id(msg, item.kind),
                kind=item.kind,
                mime=item.mime,
            )

        return [out[i] for i in range(len(items))]

    async def send_cached(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        files: list[CachedFile],
        caption: str | None = None,
    ) -> list[CachedFile]:
        """Re-send previously cached file_ids without uploading bytes.

        Mirrors `send()`'s grouping rules: photos/videos may share a
        media_group (chunked at 10); animations/documents go solo. Returns
        the input list (file_ids round-trip unchanged).
        """
        if not files:
            return []

        groupable_idx: list[tuple[int, CachedFile]] = [
            (i, f) for i, f in enumerate(files) if f.kind in _GROUPABLE
        ]
        singular_idx: list[tuple[int, CachedFile]] = [
            (i, f) for i, f in enumerate(files) if f.kind not in _GROUPABLE
        ]

        caption_used = False

        if len(groupable_idx) >= 2:
            caption_used = await self._send_cached_group(
                chat_id=chat_id,
                reply_to=reply_to,
                groupable=groupable_idx,
                caption=caption,
            )
        elif len(groupable_idx) == 1:
            _, file = groupable_idx[0]
            effective_caption = caption if not caption_used else None
            await self._send_cached_single(
                chat_id=chat_id,
                reply_to=reply_to,
                file=file,
                caption=effective_caption,
            )
            if effective_caption is not None:
                caption_used = True

        singular_reply = reply_to if not groupable_idx else None
        for _, file in singular_idx:
            effective_caption = caption if not caption_used else None
            await self._send_cached_single(
                chat_id=chat_id,
                reply_to=singular_reply,
                file=file,
                caption=effective_caption,
            )
            if effective_caption is not None:
                caption_used = True
            singular_reply = None

        return list(files)

    async def _send_cached_group(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        groupable: list[tuple[int, CachedFile]],
        caption: str | None,
    ) -> bool:
        caption_used = False
        for chunk_idx, chunk in enumerate(_chunks(groupable, _MEDIA_GROUP_MAX)):
            media_list: list[
                InputMediaAudio
                | InputMediaDocument
                | InputMediaLivePhoto
                | InputMediaPhoto
                | InputMediaVideo
            ] = []
            for slot_idx, (_, file) in enumerate(chunk):
                want_caption = (
                    chunk_idx == 0
                    and slot_idx == 0
                    and not caption_used
                    and caption is not None
                )
                cap = caption if want_caption else None
                if want_caption:
                    caption_used = True
                if file.kind == "photo":
                    media_list.append(InputMediaPhoto(media=file.file_id, caption=cap))
                else:
                    media_list.append(InputMediaVideo(media=file.file_id, caption=cap))
            chunk_reply = reply_to if chunk_idx == 0 else None

            async def factory(
                _media: list[
                    InputMediaAudio
                    | InputMediaDocument
                    | InputMediaLivePhoto
                    | InputMediaPhoto
                    | InputMediaVideo
                ] = media_list,
                _reply: int | None = chunk_reply,
            ) -> list[Message]:
                return await self._bot.send_media_group(
                    chat_id=chat_id,
                    media=_media,
                    reply_to_message_id=_reply,
                )

            await self._call_with_retry(factory, fallback_item=None)
        return caption_used

    async def _send_cached_single(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        file: CachedFile,
        caption: str | None,
    ) -> None:
        kind = file.kind
        file_id = file.file_id

        if kind == "photo":
            async def photo_factory() -> Message:
                return await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=caption,
                    reply_to_message_id=reply_to,
                )

            await self._call_with_retry(photo_factory, fallback_item=None)
            return

        if kind == "video":
            async def video_factory() -> Message:
                return await self._bot.send_video(
                    chat_id=chat_id,
                    video=file_id,
                    caption=caption,
                    supports_streaming=True,
                    reply_to_message_id=reply_to,
                )

            await self._call_with_retry(video_factory, fallback_item=None)
            return

        if kind == "animation":
            async def animation_factory() -> Message:
                return await self._bot.send_animation(
                    chat_id=chat_id,
                    animation=file_id,
                    caption=caption,
                    reply_to_message_id=reply_to,
                )

            await self._call_with_retry(animation_factory, fallback_item=None)
            return

        async def document_factory() -> Message:
            return await self._bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=caption,
                reply_to_message_id=reply_to,
            )

        await self._call_with_retry(document_factory, fallback_item=None)

    async def _send_group(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        groupable: list[tuple[int, MediaItem]],
        caption: str | None,
        out: dict[int, CachedFile],
    ) -> bool:
        caption_used = False
        for chunk_idx, chunk in enumerate(_chunks(groupable, _MEDIA_GROUP_MAX)):
            media_list: list[
                InputMediaAudio
                | InputMediaDocument
                | InputMediaLivePhoto
                | InputMediaPhoto
                | InputMediaVideo
            ] = []
            for slot_idx, (_, item) in enumerate(chunk):
                if (
                    chunk_idx == 0
                    and slot_idx == 0
                    and not caption_used
                    and caption is not None
                ):
                    media_list.append(_input_for_groupable(item, caption=caption))
                    caption_used = True
                else:
                    media_list.append(_input_for_groupable(item, caption=None))
            chunk_reply = reply_to if chunk_idx == 0 else None
            first_item = chunk[0][1]

            async def factory(
                _media: list[
                    InputMediaAudio
                    | InputMediaDocument
                    | InputMediaLivePhoto
                    | InputMediaPhoto
                    | InputMediaVideo
                ] = media_list,
                _reply: int | None = chunk_reply,
            ) -> list[Message]:
                return await self._bot.send_media_group(
                    chat_id=chat_id,
                    media=_media,
                    reply_to_message_id=_reply,
                )

            msgs = await self._call_with_retry(factory, fallback_item=first_item)
            if len(msgs) != len(chunk):
                raise RuntimeError(
                    f"send_media_group returned {len(msgs)} messages for "
                    f"{len(chunk)} items",
                )
            for (orig_idx, item), msg in zip(chunk, msgs, strict=True):
                out[orig_idx] = CachedFile(
                    file_id=_extract_file_id(msg, item.kind),
                    kind=item.kind,
                    mime=item.mime,
                )
        return caption_used

    async def _send_single(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        item: MediaItem,
        caption: str | None,
    ) -> Message:
        media = FSInputFile(str(item.path))
        kind = item.kind

        if kind == "photo":
            async def photo_factory() -> Message:
                return await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=media,
                    caption=caption,
                    reply_to_message_id=reply_to,
                )

            return await self._call_with_retry(photo_factory, fallback_item=item)

        if kind == "video":
            async def video_factory() -> Message:
                return await self._bot.send_video(
                    chat_id=chat_id,
                    video=media,
                    caption=caption,
                    width=item.width,
                    height=item.height,
                    duration=item.duration_s,
                    supports_streaming=True,
                    reply_to_message_id=reply_to,
                )

            return await self._call_with_retry(video_factory, fallback_item=item)

        if kind == "animation":
            async def animation_factory() -> Message:
                return await self._bot.send_animation(
                    chat_id=chat_id,
                    animation=media,
                    caption=caption,
                    width=item.width,
                    height=item.height,
                    duration=item.duration_s,
                    reply_to_message_id=reply_to,
                )

            return await self._call_with_retry(animation_factory, fallback_item=item)

        async def document_factory() -> Message:
            return await self._bot.send_document(
                chat_id=chat_id,
                document=media,
                caption=caption,
                reply_to_message_id=reply_to,
            )

        return await self._call_with_retry(document_factory, fallback_item=item)

    async def _call_with_retry(
        self,
        factory: Callable[[], Awaitable[T]],
        *,
        fallback_item: MediaItem | None = None,
    ) -> T:
        try:
            return await factory()
        except TelegramRetryAfter as exc:
            await self._sleep(float(exc.retry_after) + _RETRY_PAD_S)
            try:
                return await factory()
            except TelegramBadRequest as exc2:
                if _is_too_big(exc2):
                    raise MediaTooLarge(
                        f"telegram rejected upload: {exc2.message}",
                        path=_path_for(fallback_item),
                    ) from exc2
                raise
        except TelegramBadRequest as exc:
            if _is_too_big(exc):
                raise MediaTooLarge(
                    f"telegram rejected upload: {exc.message}",
                    path=_path_for(fallback_item),
                ) from exc
            raise
