from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message, MessageEntity

from yoink.extractor.urls import extract_urls, normalize_url


def _msg(text: str | None = None, entities: list[MessageEntity] | None = None,
         caption: str | None = None, caption_entities: list[MessageEntity] | None = None) -> Message:
    ns = SimpleNamespace(
        text=text,
        entities=entities,
        caption=caption,
        caption_entities=caption_entities,
    )
    return cast(Message, ns)


def _ent(typ: str, offset: int, length: int, url: str | None = None) -> MessageEntity:
    data: dict[str, Any] = {"type": typ, "offset": offset, "length": length}
    if url is not None:
        data["url"] = url
    return MessageEntity.model_validate(data)


class TestNormalizeUrl:
    def test_lowercases_scheme_and_host(self) -> None:
        assert normalize_url("HTTPS://Www.Instagram.COM/p/ABC") == "https://www.instagram.com/p/ABC"

    def test_strips_fragment(self) -> None:
        assert normalize_url("https://x.com/p/abc#frag") == "https://x.com/p/abc"

    def test_strips_igshid(self) -> None:
        assert normalize_url("https://www.instagram.com/p/ABC/?igshid=xyz123") == "https://www.instagram.com/p/ABC/"

    def test_strips_utm_and_fbclid(self) -> None:
        url = "https://example.com/x?utm_source=t&utm_medium=p&fbclid=q&keep=1"
        assert normalize_url(url) == "https://example.com/x?keep=1"

    def test_strips_si_param(self) -> None:
        assert normalize_url("https://youtu.be/abc?si=tracker") == "https://youtu.be/abc"

    def test_keeps_meaningful_query(self) -> None:
        assert normalize_url("https://x.com/search?q=cats") == "https://x.com/search?q=cats"


class TestExtractUrlsFromEntities:
    def test_url_entity(self) -> None:
        text = "look at https://www.instagram.com/p/abc"
        ents = [_ent("url", 8, len("https://www.instagram.com/p/abc"))]
        assert extract_urls(_msg(text=text, entities=ents)) == ["https://www.instagram.com/p/abc"]

    def test_text_link_entity_uses_url_not_displayed(self) -> None:
        text = "click here for fun"
        ents = [_ent("text_link", 6, 4, url="https://www.instagram.com/p/abc")]
        assert extract_urls(_msg(text=text, entities=ents)) == ["https://www.instagram.com/p/abc"]

    def test_strips_trailing_punct_in_url_entity(self) -> None:
        text = "see https://x.com/p/abc)."
        ents = [_ent("url", 4, len("https://x.com/p/abc)."))]
        assert extract_urls(_msg(text=text, entities=ents)) == ["https://x.com/p/abc"]

    def test_ignores_non_url_entities(self) -> None:
        text = "bold word"
        ents = [_ent("bold", 0, 4)]
        assert extract_urls(_msg(text=text, entities=ents)) == []

    def test_multiple_urls_preserve_order_and_dedupe(self) -> None:
        text = "https://a.com/x https://b.com/y https://a.com/x"
        ents = [
            _ent("url", 0, 15),
            _ent("url", 16, 15),
            _ent("url", 32, 15),
        ]
        assert extract_urls(_msg(text=text, entities=ents)) == ["https://a.com/x", "https://b.com/y"]

    def test_normalizes_ig_query(self) -> None:
        text = "https://www.instagram.com/p/ABC/?igshid=xyz"
        ents = [_ent("url", 0, len(text))]
        assert extract_urls(_msg(text=text, entities=ents)) == ["https://www.instagram.com/p/ABC/"]


class TestExtractUrlsRegexFallback:
    def test_plain_text_regex(self) -> None:
        text = "look https://example.com/foo bye"
        assert extract_urls(_msg(text=text)) == ["https://example.com/foo"]

    def test_strips_trailing_punct(self) -> None:
        text = "see https://x.com/p/abc)."
        assert extract_urls(_msg(text=text)) == ["https://x.com/p/abc"]

    def test_strips_trailing_quote_and_bracket(self) -> None:
        assert extract_urls(_msg(text='end "https://x.com/p/abc"')) == ["https://x.com/p/abc"]

    def test_ignores_tg_scheme(self) -> None:
        assert extract_urls(_msg(text="open tg://resolve?domain=foo and nothing")) == []

    def test_ignores_mailto(self) -> None:
        assert extract_urls(_msg(text="email me at mailto:user@example.com please")) == []

    def test_no_urls(self) -> None:
        assert extract_urls(_msg(text="just some words")) == []

    def test_entities_take_precedence_over_regex(self) -> None:
        text = "https://a.com/x and https://b.com/y"
        ents = [_ent("url", 0, 15)]
        assert extract_urls(_msg(text=text, entities=ents)) == ["https://a.com/x"]


class TestExtractUrlsCaption:
    def test_reads_caption_when_no_text(self) -> None:
        cap = "photo https://example.com/p"
        assert extract_urls(_msg(caption=cap)) == ["https://example.com/p"]

    def test_uses_caption_entities(self) -> None:
        cap = "click here"
        ents = [_ent("text_link", 0, 5, url="https://example.com/p")]
        assert extract_urls(_msg(caption=cap, caption_entities=ents)) == ["https://example.com/p"]


class TestExtractUrlsEmpty:
    def test_empty_message(self) -> None:
        assert extract_urls(_msg()) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://x.com/p/abc).", "https://x.com/p/abc"),
        ("https://x.com/p/abc,", "https://x.com/p/abc"),
        ("https://x.com/p/abc!", "https://x.com/p/abc"),
        ("https://x.com/p/abc?q=1", "https://x.com/p/abc?q=1"),
    ],
)
def test_trailing_punct_strip_parametrized(raw: str, expected: str) -> None:
    text = f"see {raw} ok"
    assert extract_urls(_msg(text=text)) == [expected]
