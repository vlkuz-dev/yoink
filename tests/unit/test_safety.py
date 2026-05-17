from __future__ import annotations

import pytest

from yoink.downloader.safety import (
    UnsafeURLError,
    redact_url,
    sanitize_filename,
    validate_url,
)


def _resolver(mapping: dict[str, list[str]]):
    def _resolve(host: str, port: int) -> list[str]:
        del port
        if host in mapping:
            return mapping[host]
        raise OSError(f"no fixture for {host}")

    return _resolve


class TestValidateURLSchemes:
    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_url("file:///etc/passwd")

    def test_rejects_ftp(self) -> None:
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_url("ftp://example.com/x")

    def test_rejects_javascript(self) -> None:
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_url("javascript:alert(1)")

    def test_rejects_empty(self) -> None:
        with pytest.raises(UnsafeURLError):
            validate_url("")


class TestValidateURLHostAndUserinfo:
    def test_rejects_missing_host(self) -> None:
        with pytest.raises(UnsafeURLError, match="host"):
            validate_url("https:///foo")

    def test_rejects_userinfo(self) -> None:
        with pytest.raises(UnsafeURLError, match="userinfo"):
            validate_url(
                "https://user:pwd@www.instagram.com/p/abc",
                resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
            )

    def test_rejects_userinfo_username_only(self) -> None:
        with pytest.raises(UnsafeURLError, match="userinfo"):
            validate_url(
                "https://bob@www.instagram.com/p/abc",
                resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
            )


class TestValidateURLPorts:
    def test_accepts_default_https(self) -> None:
        result = validate_url(
            "https://www.instagram.com/p/abc",
            resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
        )
        assert result.port == 443

    def test_accepts_explicit_443(self) -> None:
        result = validate_url(
            "https://www.instagram.com:443/p/abc",
            resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
        )
        assert result.port == 443

    def test_rejects_nonstandard_port(self) -> None:
        with pytest.raises(UnsafeURLError, match="port"):
            validate_url(
                "https://www.instagram.com:8443/p/abc",
                resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
            )

    def test_allowed_ports_override(self) -> None:
        result = validate_url(
            "https://www.instagram.com:8443/p/abc",
            allowed_ports=frozenset({443, 8443}),
            resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
        )
        assert result.port == 8443


class TestValidateURLLiteralIPs:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x",
            "http://10.0.0.1/x",
            "http://172.16.5.5/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/x",
            "http://0.0.0.0/x",
            "http://224.0.0.1/x",
            "http://[::1]/x",
            "http://[fc00::1]/x",
            "http://[fe80::1]/x",
            "http://[ff00::1]/x",
        ],
    )
    def test_rejects_unsafe_literal_ip(self, url: str) -> None:
        with pytest.raises(UnsafeURLError, match="unsafe"):
            validate_url(url)

    def test_accepts_public_literal_ip(self) -> None:
        result = validate_url("https://1.1.1.1/x")
        assert result.resolved_ips == ("1.1.1.1",)
        assert result.host == "1.1.1.1"


class TestValidateURLDNSResolution:
    def test_accepts_public_host(self) -> None:
        result = validate_url(
            "https://www.instagram.com/p/abc",
            resolver=_resolver({"www.instagram.com": ["157.240.22.174"]}),
        )
        assert result.host == "www.instagram.com"
        assert result.scheme == "https"
        assert result.resolved_ips == ("157.240.22.174",)

    def test_rejects_when_any_resolved_ip_is_private(self) -> None:
        with pytest.raises(UnsafeURLError, match="unsafe"):
            validate_url(
                "https://evil.example.com/x",
                resolver=_resolver(
                    {"evil.example.com": ["157.240.22.174", "10.0.0.5"]},
                ),
            )

    def test_rejects_when_dns_returns_empty(self) -> None:
        with pytest.raises(UnsafeURLError, match="no DNS results"):
            validate_url(
                "https://nothing.example.com/x",
                resolver=_resolver({"nothing.example.com": []}),
            )

    def test_rejects_when_resolver_fails(self) -> None:
        def boom(host: str, port: int) -> list[str]:
            del host, port
            raise OSError("nope")

        with pytest.raises(UnsafeURLError, match="DNS resolution failed"):
            validate_url("https://gone.example.com/x", resolver=boom)


class TestValidateURLAllowlist:
    def test_accepts_exact_match(self) -> None:
        result = validate_url(
            "https://instagram.com/p/abc",
            allowlist=frozenset({"instagram.com"}),
            resolver=_resolver({"instagram.com": ["1.2.3.4"]}),
        )
        assert result.host == "instagram.com"

    def test_accepts_subdomain(self) -> None:
        result = validate_url(
            "https://www.instagram.com/p/abc",
            allowlist=frozenset({"instagram.com"}),
            resolver=_resolver({"www.instagram.com": ["1.2.3.4"]}),
        )
        assert result.host == "www.instagram.com"

    def test_rejects_outside_allowlist(self) -> None:
        with pytest.raises(UnsafeURLError, match="allowlist"):
            validate_url(
                "https://evil.com/x",
                allowlist=frozenset({"instagram.com"}),
                resolver=_resolver({"evil.com": ["1.2.3.4"]}),
            )

    def test_rejects_lookalike_suffix(self) -> None:
        with pytest.raises(UnsafeURLError, match="allowlist"):
            validate_url(
                "https://notinstagram.com/x",
                allowlist=frozenset({"instagram.com"}),
                resolver=_resolver({"notinstagram.com": ["1.2.3.4"]}),
            )

    def test_empty_allowlist_blocks_everything(self) -> None:
        with pytest.raises(UnsafeURLError, match="allowlist"):
            validate_url(
                "https://instagram.com/x",
                allowlist=frozenset(),
                resolver=_resolver({"instagram.com": ["1.2.3.4"]}),
            )


class TestSanitizeFilename:
    def test_strips_path_separators(self) -> None:
        assert "/" not in sanitize_filename("../../etc/passwd")
        assert "\\" not in sanitize_filename("a\\b\\c.jpg")

    def test_strips_null_and_control(self) -> None:
        out = sanitize_filename("hello\x00world\x07.jpg")
        assert "\x00" not in out
        assert "\x07" not in out

    def test_strips_leading_dots(self) -> None:
        assert not sanitize_filename("...hidden").startswith(".")

    def test_strips_leading_spaces(self) -> None:
        assert not sanitize_filename("   spaced.jpg").startswith(" ")

    def test_limits_length(self) -> None:
        long = "a" * 500 + ".jpg"
        assert len(sanitize_filename(long)) == 200

    def test_replaces_empty_with_placeholder(self) -> None:
        assert sanitize_filename("...") == "_"
        assert sanitize_filename("") == "_"

    def test_nfkd_normalizes(self) -> None:
        # NFKD splits "é" (U+00E9) into "e" + combining accent
        result = sanitize_filename("café.jpg")
        assert "caf" in result
        assert result.endswith(".jpg")

    def test_keeps_normal_filename(self) -> None:
        assert sanitize_filename("photo_01.jpg") == "photo_01.jpg"


class TestRedactURL:
    def test_strips_userinfo(self) -> None:
        out = redact_url("http://user:pass@example.com/path")
        assert "user" not in out
        assert "pass" not in out
        assert "example.com" in out

    def test_handles_malformed_port(self) -> None:
        # Malformed port must not crash the redactor — pipeline logs
        # rejected URLs through this helper, and a crash there would
        # mask the original UnsafeURLError.
        out = redact_url("http://user:pass@example.com:abc/path")
        assert "user" not in out
        assert "pass" not in out
        assert "example.com" in out

    def test_preserves_url_without_secrets(self) -> None:
        assert redact_url("https://example.com/x") == "https://example.com/x"

    def test_scrubs_query_string(self) -> None:
        out = redact_url("https://example.com/cb?access_token=abc123&state=xyz")
        assert "abc123" not in out
        assert "access_token" not in out
        assert "state" not in out
        assert "example.com" in out
        assert "/cb" in out
        assert "REDACTED" in out

    def test_scrubs_fragment(self) -> None:
        out = redact_url("https://example.com/p#token=secret")
        assert "secret" not in out
        assert "token" not in out
        assert "example.com" in out
        assert "REDACTED" in out

    def test_scrubs_query_with_userinfo(self) -> None:
        out = redact_url("https://u:p@example.com/cb?sig=topsecret")
        assert "u" not in out.split("//", 1)[1].split("/", 1)[0]
        assert "p@" not in out
        assert "topsecret" not in out
        assert "example.com" in out

    def test_handles_empty(self) -> None:
        assert redact_url("") == "<empty>"
