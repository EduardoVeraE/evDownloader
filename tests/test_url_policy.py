from __future__ import annotations

import pytest

from evdownloader import browser
from evdownloader.extractors import get_extractor
from evdownloader.extractors.codigofacilito import CodigofacilitoExtractor
from evdownloader.extractors.platzi import PlatziExtractor, _is_media_request
from evdownloader.extractors.udemy import UdemyExtractor
from evdownloader.models import Cookie
from evdownloader.resource_download import _trusted_url
from evdownloader.url_policy import URLPolicy, parse_url_authority

_PROVIDERS = [
    (PlatziExtractor, "platzi.com"),
    (UdemyExtractor, "udemy.com"),
    (CodigofacilitoExtractor, "codigofacilito.com"),
]


@pytest.mark.parametrize(("extractor", "domain"), _PROVIDERS)
@pytest.mark.parametrize("template", ["https://{}", "https://www.{}:443/course"])
def test_provider_supports_normalized_authorities(extractor, domain: str, template: str) -> None:
    assert extractor.supports(template.format(domain.upper()))


@pytest.mark.parametrize(("extractor", "domain"), _PROVIDERS)
@pytest.mark.parametrize(
    "template",
    [
        "https://{}.evil.test/course",
        "https://evil{}/course",
        "https://user:password@{}/course",
        "https://{}./course",
        "https://{}:/course",
        "https://{}:0/course",
        "https://{}:444/course",
        "http://{}/course",
        "ftp://{}/course",
        "https://{}%2f.evil.test/course",
        "https://{}%40evil.test/course",
        "https://{}\\@evil.test/course",
    ],
)
def test_provider_supports_rejects_authority_confusion(
    extractor, domain: str, template: str
) -> None:
    assert not extractor.supports(template.format(domain))


@pytest.mark.parametrize(("extractor", "domain"), _PROVIDERS)
def test_registry_uses_the_same_provider_policy(extractor, domain: str) -> None:
    assert isinstance(get_extractor(f"https://www.{domain}/course"), extractor)
    with pytest.raises(ValueError) as error:
        get_extractor(f"https://{domain}.evil.test/course?token=synthetic-secret")
    assert "synthetic-secret" not in str(error.value)
    assert domain not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https:///missing-host",
        "https://example.test:bad/path",
        "https://[not-ipv6]/path",
        "https://example%2etest/path",
        "https://example.test\\evil.test/path",
        "https://example.test%2f@evil.test/path",
        "https://example.test./path",
        "https://example.test\u3002/path",
    ],
)
def test_authority_parser_fails_closed(url: str) -> None:
    assert parse_url_authority(url) is None


def test_authority_parser_normalizes_idna_and_default_port() -> None:
    authority = parse_url_authority("https://b\u00fccher.example:443/path")
    assert authority is not None
    assert authority.hostname == "xn--bcher-kva.example"
    assert authority.port == 443
    assert authority.explicit_port is True
    assert URLPolicy("synthetic", ("xn--bcher-kva.example",)).allows_authority(authority)


def test_authority_parser_rejects_explicit_zero_globally() -> None:
    assert parse_url_authority("https://provider.test:0/path") is None


def test_authority_parser_preserves_valid_external_port() -> None:
    authority = parse_url_authority("https://license.external.test:8443/path")
    assert authority is not None
    assert authority.port == 8443
    assert authority.explicit_port is True


def test_authority_policy_matches_ip_addresses_exactly() -> None:
    assert URLPolicy("synthetic", ("127.0.0.1",)).allows("https://127.0.0.1/path")
    assert not URLPolicy("synthetic", ("0.0.1",)).allows("https://127.0.0.1/path")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://udemycdn.com/file", True),
        ("https://cdn.udemycdn.com/file", True),
        ("https://udemycdn.com.evil.test/file", False),
        ("https://eviludemycdn.com/file", False),
        ("https://udemycdn.com:444/file", False),
        ("https://udemycdn.com:0/file", False),
        ("https://udemycdn.com./file", False),
        ("https://user@udemycdn.com/file", False),
    ],
)
def test_resource_cdn_policy_uses_label_boundaries(url: str, expected: bool) -> None:
    assert _trusted_url(url, ("udemycdn.com",)) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mdstrm.com/embed/id", True),
        ("https://cdn.mdstrm.com/video/master.m3u8", True),
        ("https://mdstrm.com.evil.test/video/master.m3u8", False),
        ("https://evilmdstrm.com/video/master.m3u8", False),
        ("https://mdstrm.com:444/video/master.m3u8", False),
        ("https://mdstrm.com:0/video/master.m3u8", False),
        ("https://mdstrm.com./video/master.m3u8", False),
    ],
)
def test_platzi_media_policy_uses_label_boundaries(url: str, expected: bool) -> None:
    assert _is_media_request(url) is expected


def test_cookie_forwarding_intersects_rfc_scope_with_trusted_boundary() -> None:
    cookies = [
        Cookie(name="provider", value="one", domain=".provider.test", path="/private"),
        Cookie(name="other", value="two", domain=".other.test", path="/"),
    ]
    assert (
        browser.cookie_header_for_url(
            cookies,
            "https://api.provider.test/private/file",
            trusted_host_suffixes=("provider.test",),
            now=100,
        )
        == "provider=one"
    )
    assert (
        browser.cookie_header_for_url(
            cookies,
            "https://other.test/private/file",
            trusted_host_suffixes=("provider.test",),
            now=100,
        )
        is None
    )


@pytest.mark.parametrize(
    "url",
    ["https://provider.test:0/private", "https://provider.test./private"],
)
def test_cookie_forwarding_rejects_zero_port_and_trailing_dot(url: str) -> None:
    cookie = Cookie(name="session", value="synthetic", domain=".provider.test")
    assert (
        browser.cookie_header_for_url(
            [cookie], url, trusted_host_suffixes=("provider.test",), now=100
        )
        is None
    )


def test_cookiefile_filter_rejects_expired_malformed_and_cross_boundary_records() -> None:
    valid = Cookie(name="session", value="valid", domain=".provider.test")
    records = [
        valid,
        Cookie(name="expired", value="old", domain=".provider.test", expires=99),
        Cookie(name="bad", value="line\nbreak", domain=".provider.test"),
        Cookie(name="other", value="third-party", domain=".other.test"),
    ]
    assert browser.filter_cookie_records(records, ("provider.test",), now=100) == [valid]
