from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from aiogram.types import Message, MessageEntity

_TRAILING_PUNCT = ".,;:!?)]}>\"'"

_URL_REGEX = re.compile(
    r"""https?://[^\s<>"'`]+""",
    re.IGNORECASE,
)

_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)
_TRACKING_PARAMS_EXACT: frozenset[str] = frozenset({"igshid", "si", "fbclid", "gclid", "yclid"})

_SCHEME_BLOCKLIST: frozenset[str] = frozenset({"tg", "mailto", "tel", "javascript", "data", "file", "ftp"})


def _strip_trailing_punct(s: str) -> str:
    while s and s[-1] in _TRAILING_PUNCT:
        s = s[:-1]
    return s


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    if k in _TRACKING_PARAMS_EXACT:
        return True
    return any(k.startswith(p) for p in _TRACKING_PARAM_PREFIXES)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)]
    new_query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, parts.path, new_query, ""))


def _scheme_of(url: str) -> str:
    head, sep, _ = url.partition(":")
    if not sep:
        return ""
    return head.lower()


def _from_entities(text: str, entities: list[MessageEntity]) -> list[str]:
    out: list[str] = []
    for ent in entities:
        if ent.type == "url":
            piece = ent.extract_from(text)
            out.append(_strip_trailing_punct(piece))
        elif ent.type == "text_link" and ent.url:
            out.append(ent.url)
    return out


def _regex_fallback(text: str) -> list[str]:
    return [_strip_trailing_punct(m) for m in _URL_REGEX.findall(text)]


def extract_urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    entities = list(message.entities or message.caption_entities or [])

    raw: list[str] = _from_entities(text, entities) if entities else []
    raw.extend(_regex_fallback(text))

    seen: set[str] = set()
    out: list[str] = []
    for u in raw:
        if not u:
            continue
        scheme = _scheme_of(u)
        if scheme in _SCHEME_BLOCKLIST:
            continue
        if scheme not in {"http", "https"}:
            continue
        normalized = normalize_url(u)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
