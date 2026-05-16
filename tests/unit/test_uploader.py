from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    Animation,
    Chat,
    Document,
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    PhotoSize,
    Video,
)

from yoink.cache.store import CachedFile
from yoink.core.errors import MediaTooLarge
from yoink.core.models import MediaItem, MediaPackage
from yoink.uploader.telegram import TelegramUploader

_CHAT_ID = 12345
_REPLY_TO = 9999


class _Method:
    """Stand-in TelegramMethod for exception construction."""


def _make_chat() -> Chat:
    return Chat(id=_CHAT_ID, type="private")


def _photo_message(file_id: str, *, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=_make_chat(),
        photo=[
            PhotoSize(file_id=f"{file_id}_small", file_unique_id=f"{file_id}_us", width=10, height=10),
            PhotoSize(file_id=file_id, file_unique_id=f"{file_id}_ub", width=200, height=200),
        ],
    )


def _video_message(file_id: str, *, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=_make_chat(),
        video=Video(
            file_id=file_id,
            file_unique_id=f"{file_id}_u",
            width=1280,
            height=720,
            duration=10,
        ),
    )


def _animation_message(file_id: str, *, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=_make_chat(),
        animation=Animation(
            file_id=file_id,
            file_unique_id=f"{file_id}_u",
            width=320,
            height=240,
            duration=2,
        ),
    )


def _document_message(file_id: str, *, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=_make_chat(),
        document=Document(file_id=file_id, file_unique_id=f"{file_id}_u"),
    )


def _photo_item(tmp_path: Path, name: str) -> MediaItem:
    p = tmp_path / f"{name}.jpg"
    p.write_bytes(b"x")
    return MediaItem(path=p, kind="photo", mime="image/jpeg")


def _video_item(tmp_path: Path, name: str) -> MediaItem:
    p = tmp_path / f"{name}.mp4"
    p.write_bytes(b"x")
    return MediaItem(
        path=p,
        kind="video",
        mime="video/mp4",
        width=1280,
        height=720,
        duration_s=10,
    )


def _animation_item(tmp_path: Path, name: str) -> MediaItem:
    p = tmp_path / f"{name}.gif"
    p.write_bytes(b"x")
    return MediaItem(path=p, kind="animation", mime="image/gif")


def _document_item(tmp_path: Path, name: str) -> MediaItem:
    p = tmp_path / f"{name}.pdf"
    p.write_bytes(b"x")
    return MediaItem(path=p, kind="document", mime="application/pdf")


def _make_bot(
    *,
    photo_results: list[Message] | None = None,
    video_results: list[Message] | None = None,
    animation_results: list[Message] | None = None,
    document_results: list[Message] | None = None,
    media_group_results: list[list[Message]] | None = None,
) -> AsyncMock:
    bot = AsyncMock()
    bot.send_photo = AsyncMock(side_effect=list(photo_results or []))
    bot.send_video = AsyncMock(side_effect=list(video_results or []))
    bot.send_animation = AsyncMock(side_effect=list(animation_results or []))
    bot.send_document = AsyncMock(side_effect=list(document_results or []))
    bot.send_media_group = AsyncMock(side_effect=list(media_group_results or []))
    return bot


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_single_photo_returns_one_file_id(tmp_path: Path) -> None:
    item = _photo_item(tmp_path, "a")
    bot = _make_bot(photo_results=[_photo_message("PHOTO_ID")])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x/p/1", provider="ig", items=[item])
    out = await uploader.send(_CHAT_ID, _REPLY_TO, pkg)

    assert out == [CachedFile(file_id="PHOTO_ID", kind="photo", mime="image/jpeg")]
    bot.send_photo.assert_awaited_once()
    call_kwargs = bot.send_photo.await_args.kwargs
    assert call_kwargs["chat_id"] == _CHAT_ID
    assert call_kwargs["caption"] is None
    assert call_kwargs["reply_to_message_id"] == _REPLY_TO
    bot.send_media_group.assert_not_called()


@pytest.mark.asyncio
async def test_single_video_returns_file_id(tmp_path: Path) -> None:
    item = _video_item(tmp_path, "v")
    bot = _make_bot(video_results=[_video_message("VIDEO_ID")])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x/reel/1", provider="ig", items=[item])
    out = await uploader.send(_CHAT_ID, None, pkg)

    assert out == [CachedFile(file_id="VIDEO_ID", kind="video", mime="video/mp4")]
    bot.send_video.assert_awaited_once()
    kwargs = bot.send_video.await_args.kwargs
    assert kwargs["width"] == 1280
    assert kwargs["height"] == 720
    assert kwargs["duration"] == 10


@pytest.mark.asyncio
async def test_album_of_three_photos_uses_one_media_group(tmp_path: Path) -> None:
    items = [_photo_item(tmp_path, f"p{i}") for i in range(3)]
    msgs = [_photo_message(f"P{i}", message_id=i + 1) for i in range(3)]
    bot = _make_bot(media_group_results=[msgs])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(
        source_url="https://x/p/abc",
        provider="ig",
        items=items,
        caption="hello",
    )
    out = await uploader.send(_CHAT_ID, _REPLY_TO, pkg)

    assert [c.file_id for c in out] == ["P0", "P1", "P2"]
    bot.send_media_group.assert_awaited_once()
    bot.send_photo.assert_not_called()
    call_kwargs = bot.send_media_group.await_args.kwargs
    media: list[Any] = call_kwargs["media"]
    assert len(media) == 3
    assert all(isinstance(m, InputMediaPhoto) for m in media)
    assert media[0].caption == "hello"
    assert media[1].caption is None
    assert media[2].caption is None
    assert call_kwargs["reply_to_message_id"] == _REPLY_TO


@pytest.mark.asyncio
async def test_album_of_fifteen_chunks_into_ten_plus_five(tmp_path: Path) -> None:
    items = [_photo_item(tmp_path, f"p{i:02d}") for i in range(15)]
    first_msgs = [_photo_message(f"A{i:02d}", message_id=i + 1) for i in range(10)]
    second_msgs = [_photo_message(f"B{i:02d}", message_id=i + 11) for i in range(5)]
    bot = _make_bot(media_group_results=[first_msgs, second_msgs])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(
        source_url="https://x/p/abc",
        provider="ig",
        items=items,
        caption="long",
    )
    out = await uploader.send(_CHAT_ID, _REPLY_TO, pkg)

    expected_ids = [f"A{i:02d}" for i in range(10)] + [f"B{i:02d}" for i in range(5)]
    assert [c.file_id for c in out] == expected_ids
    assert bot.send_media_group.await_count == 2

    first_call = bot.send_media_group.await_args_list[0].kwargs
    second_call = bot.send_media_group.await_args_list[1].kwargs
    assert len(first_call["media"]) == 10
    assert len(second_call["media"]) == 5
    assert first_call["media"][0].caption == "long"
    assert all(m.caption is None for m in first_call["media"][1:])
    assert all(m.caption is None for m in second_call["media"])
    assert first_call["reply_to_message_id"] == _REPLY_TO
    assert second_call["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_mixed_kind_splits_group_and_animation(tmp_path: Path) -> None:
    items = [
        _photo_item(tmp_path, "p0"),
        _photo_item(tmp_path, "p1"),
        _animation_item(tmp_path, "a0"),
    ]
    group_msgs = [_photo_message("P0", message_id=1), _photo_message("P1", message_id=2)]
    bot = _make_bot(
        media_group_results=[group_msgs],
        animation_results=[_animation_message("ANIM0", message_id=3)],
    )
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=items)
    out = await uploader.send(_CHAT_ID, None, pkg)

    assert [c.file_id for c in out] == ["P0", "P1", "ANIM0"]
    bot.send_media_group.assert_awaited_once()
    bot.send_animation.assert_awaited_once()
    bot.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_group_supports_video_with_photos(tmp_path: Path) -> None:
    items = [
        _photo_item(tmp_path, "p0"),
        _video_item(tmp_path, "v0"),
    ]
    group_msgs = [_photo_message("P0", message_id=1), _video_message("V0", message_id=2)]
    bot = _make_bot(media_group_results=[group_msgs])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=items)
    out = await uploader.send(_CHAT_ID, None, pkg)

    assert [c.file_id for c in out] == ["P0", "V0"]
    bot.send_media_group.assert_awaited_once()
    media: list[Any] = bot.send_media_group.await_args.kwargs["media"]
    assert isinstance(media[0], InputMediaPhoto)
    assert isinstance(media[1], InputMediaVideo)


@pytest.mark.asyncio
async def test_caption_on_first_singular_when_no_group(tmp_path: Path) -> None:
    items = [_animation_item(tmp_path, "a0"), _document_item(tmp_path, "d0")]
    bot = _make_bot(
        animation_results=[_animation_message("ANIM0", message_id=1)],
        document_results=[_document_message("DOC0", message_id=2)],
    )
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=items, caption="cap")
    out = await uploader.send(_CHAT_ID, None, pkg)

    assert [c.file_id for c in out] == ["ANIM0", "DOC0"]
    assert bot.send_animation.await_args.kwargs["caption"] == "cap"
    assert bot.send_document.await_args.kwargs["caption"] is None


@pytest.mark.asyncio
async def test_retry_after_triggers_sleep_then_succeeds(tmp_path: Path) -> None:
    item = _photo_item(tmp_path, "p")
    err = TelegramRetryAfter(method=_Method(), message="rate", retry_after=4)
    bot = _make_bot(photo_results=[err, _photo_message("PHOTO_ID")])
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    uploader = TelegramUploader(bot, sleep=fake_sleep)
    pkg = MediaPackage(source_url="https://x", provider="ig", items=[item])
    out = await uploader.send(_CHAT_ID, None, pkg)

    assert out == [CachedFile(file_id="PHOTO_ID", kind="photo", mime="image/jpeg")]
    assert sleeps == [4.5]
    assert bot.send_photo.await_count == 2


@pytest.mark.asyncio
async def test_too_big_raises_media_too_large(tmp_path: Path) -> None:
    item = _video_item(tmp_path, "v")
    bad = TelegramBadRequest(method=_Method(), message="File is too big")
    bot = _make_bot(video_results=[bad])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=[item])
    with pytest.raises(MediaTooLarge) as ei:
        await uploader.send(_CHAT_ID, None, pkg)
    assert ei.value.path == item.path


@pytest.mark.asyncio
async def test_too_big_after_retry_raises_media_too_large(tmp_path: Path) -> None:
    item = _photo_item(tmp_path, "p")
    retry = TelegramRetryAfter(method=_Method(), message="rate", retry_after=1)
    too_big = TelegramBadRequest(method=_Method(), message="File is too big")
    bot = _make_bot(photo_results=[retry, too_big])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=[item])
    with pytest.raises(MediaTooLarge):
        await uploader.send(_CHAT_ID, None, pkg)
    assert bot.send_photo.await_count == 2


@pytest.mark.asyncio
async def test_unrelated_bad_request_propagates(tmp_path: Path) -> None:
    item = _photo_item(tmp_path, "p")
    bad = TelegramBadRequest(method=_Method(), message="chat not found")
    bot = _make_bot(photo_results=[bad])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=[item])
    with pytest.raises(TelegramBadRequest):
        await uploader.send(_CHAT_ID, None, pkg)


@pytest.mark.asyncio
async def test_input_media_kinds_built_correctly(tmp_path: Path) -> None:
    items = [
        _photo_item(tmp_path, "p0"),
        _video_item(tmp_path, "v0"),
        _photo_item(tmp_path, "p1"),
    ]
    msgs = [
        _photo_message("P0", message_id=1),
        _video_message("V0", message_id=2),
        _photo_message("P1", message_id=3),
    ]
    bot = _make_bot(media_group_results=[msgs])
    uploader = TelegramUploader(bot, sleep=_noop_sleep)

    pkg = MediaPackage(source_url="https://x", provider="ig", items=items)
    out = await uploader.send(_CHAT_ID, None, pkg)
    assert [c.kind for c in out] == ["photo", "video", "photo"]

    media: list[Any] = bot.send_media_group.await_args.kwargs["media"]
    assert isinstance(media[0], InputMediaPhoto)
    assert isinstance(media[1], InputMediaVideo)
    assert isinstance(media[2], InputMediaPhoto)
    # not animation/document
    assert not isinstance(media[0], (InputMediaAnimation, InputMediaDocument))


@pytest.mark.asyncio
async def test_empty_package_returns_empty(tmp_path: Path) -> None:
    bot = _make_bot()
    uploader = TelegramUploader(bot, sleep=_noop_sleep)
    pkg = MediaPackage(source_url="https://x", provider="ig", items=[])
    out = await uploader.send(_CHAT_ID, None, pkg)
    assert out == []
    bot.send_photo.assert_not_called()
    bot.send_media_group.assert_not_called()
