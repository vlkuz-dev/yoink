from __future__ import annotations

import ipaddress
import socket
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable

_DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"http": 80, "https": 443}

_UNSAFE_V4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("224.0.0.0/4"),
)

_UNSAFE_V6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("ff00::/8"),
)

_FILENAME_MAX_LEN = 200
_FILENAME_REPLACEMENT = "_"


class UnsafeURLError(ValueError):
    """Raised when a URL fails safety validation."""


class Resolver(Protocol):
    def __call__(self, host: str, port: int) -> Iterable[str]: ...


@dataclass(slots=True, frozen=True, kw_only=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    resolved_ips: tuple[str, ...]


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        if isinstance(ip, str) and ip and ip not in out:
            out.append(ip)
    return out


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_unsafe_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in net for net in _UNSAFE_V4_NETWORKS)
    return any(addr in net for net in _UNSAFE_V6_NETWORKS)


def _host_in_allowlist(host: str, allowlist: frozenset[str]) -> bool:
    h = host.lower().lstrip(".")
    for entry in allowlist:
        d = entry.lower().lstrip(".")
        if not d:
            continue
        if h == d or h.endswith("." + d):
            return True
    return False


def validate_url(
    url: str,
    allowlist: frozenset[str] | None = None,
    *,
    allowed_ports: frozenset[int] = _DEFAULT_ALLOWED_PORTS,
    resolver: Resolver | None = None,
) -> ValidatedURL:
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("empty or non-string URL")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_SCHEME_PORTS:
        raise UnsafeURLError(f"scheme not allowed: {scheme!r}")

    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("userinfo (user:pass@) is not allowed")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("missing host")
    host = host.lower()

    try:
        port = parts.port if parts.port is not None else _DEFAULT_SCHEME_PORTS[scheme]
    except ValueError as exc:
        raise UnsafeURLError(f"invalid port: {exc}") from exc
    if port not in allowed_ports:
        raise UnsafeURLError(f"port not allowed: {port}")

    if allowlist is not None and not _host_in_allowlist(host, allowlist):
        raise UnsafeURLError(f"host not in allowlist: {host}")

    resolved: tuple[str, ...]
    literal_ip = _parse_ip(host)
    if literal_ip is not None:
        if _is_unsafe_ip(literal_ip):
            raise UnsafeURLError(f"literal IP host in unsafe range: {host}")
        resolved = (str(literal_ip),)
    else:
        resolve = resolver if resolver is not None else _default_resolver
        try:
            raw_ips = list(resolve(host, port))
        except OSError as exc:
            raise UnsafeURLError(f"DNS resolution failed for {host}: {exc}") from exc
        if not raw_ips:
            raise UnsafeURLError(f"no DNS results for {host}")
        parsed_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for ip in raw_ips:
            addr = _parse_ip(ip)
            if addr is None:
                raise UnsafeURLError(f"unparseable resolved address: {ip!r}")
            parsed_ips.append(addr)
        for addr in parsed_ips:
            if _is_unsafe_ip(addr):
                raise UnsafeURLError(f"{host} resolves to unsafe address: {addr}")
        resolved = tuple(str(a) for a in parsed_ips)

    return ValidatedURL(
        url=url,
        scheme=scheme,
        host=host,
        port=port,
        resolved_ips=resolved,
    )


def sanitize_filename(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("filename must be a string")
    cleaned = unicodedata.normalize("NFKD", name)
    out_chars: list[str] = []
    for ch in cleaned:
        codepoint = ord(ch)
        if codepoint < 0x20 or codepoint == 0x7F:
            out_chars.append(_FILENAME_REPLACEMENT)
            continue
        if ch in ("/", "\\", "\x00"):
            out_chars.append(_FILENAME_REPLACEMENT)
            continue
        out_chars.append(ch)
    sanitized = "".join(out_chars).lstrip(". ")
    if not sanitized:
        sanitized = _FILENAME_REPLACEMENT
    if len(sanitized) > _FILENAME_MAX_LEN:
        sanitized = sanitized[:_FILENAME_MAX_LEN]
    return sanitized
