from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader import browser
from evdownloader.drm.license import (
    LicenseInputError,
    LicensePostError,
    normalize_widevine_license_input,
    post_license_challenge,
)
from evdownloader.extractors import get_extractor
from evdownloader.extractors.codigofacilito import CodigofacilitoExtractor
from evdownloader.extractors.platzi import PlatziExtractor, _is_media_request
from evdownloader.extractors.udemy import UdemyExtractor
from evdownloader.models import Cookie, DrmInfo
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


class _Response:
    def __init__(self, status: int, *, location: str | None = None, text: str = "body") -> None:
        self.status_code = SimpleNamespace(
            as_int=lambda: status, is_success=lambda: 200 <= status < 300
        )
        self.headers = {"location": location} if location else {}
        self.text = AsyncMock(return_value=text)
        self.bytes = AsyncMock(return_value=b"license")
        self.close = AsyncMock()


@pytest.mark.asyncio
async def test_udemy_authenticated_redirect_stops_before_untrusted_host() -> None:
    response = _Response(302, location="https://evil.test/final?token=synthetic")
    extractor = UdemyExtractor()
    extractor._load_cookies = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "name": "session",
                "value": "synthetic",
                "domain": ".udemy.com",
                "path": "/",
                "secure": True,
            }
        ]
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    extractor._client = client

    assert await extractor._fetch_text("https://www.udemy.com/course/start") == ""
    assert client.get.await_count == 1
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    ["https://www.udemy.com:0/course/start", "https://www.udemy.com./course/start"],
)
async def test_udemy_authenticated_request_rejects_invalid_provider_authority(
    url: str,
) -> None:
    extractor = UdemyExtractor()
    extractor._load_cookies = MagicMock(  # type: ignore[method-assign]
        return_value=[{"name": "session", "value": "synthetic", "domain": ".udemy.com"}]
    )
    client = MagicMock()
    client.get = AsyncMock()
    extractor._client = client

    assert await extractor._fetch_text(url) == ""
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_codigofacilito_authenticated_redirect_stops_before_untrusted_host() -> None:
    response = _Response(302, location="https://codigofacilito.com.evil.test/final")
    extractor = CodigofacilitoExtractor()
    extractor._cookie_jar = [
        Cookie(name="session", value="synthetic", domain=".codigofacilito.com")
    ]
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    extractor._client = client

    with pytest.raises(ValueError, match="fuera del proveedor"):
        await extractor._fetch("https://codigofacilito.com/course/start")
    assert client.get.await_count == 1
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_platzi_rejects_untrusted_final_navigation_url() -> None:
    page = MagicMock()
    page.goto = AsyncMock(return_value=SimpleNamespace(url="https://platzi.com.evil.test/final"))
    page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    with pytest.raises(ValueError, match="redirect fuera del proveedor"):
        await PlatziExtractor().list_course(context, "https://platzi.com/courses/test")
    page.close.assert_awaited_once_with()


def _drm(**updates: object) -> DrmInfo:
    values: dict[str, object] = {
        "scheme": "widevine",
        "license_url": "https://www.udemy.com/license",
        "pssh": "AAAA",
        "token": "provider-token",
        "headers": {"Authorization": "Bearer provider", "X-Public": "provider"},
    }
    values.update(updates)
    return DrmInfo(**values)  # type: ignore[arg-type]


def test_external_drm_override_drops_provider_credentials() -> None:
    result = normalize_widevine_license_input(
        _drm(),
        override_license_url="https://license.external.test:8443/path",
        extra_headers={"Cookie": "provider=synthetic"},
        trusted_host_suffixes=("udemy.com",),
    )
    assert result.token is None
    assert result.headers == {}


def test_external_drm_override_preserves_explicit_user_token_only() -> None:
    result = normalize_widevine_license_input(
        _drm(),
        override_license_url="https://license.external.test/path",
        override_token="explicit-token",
        trusted_host_suffixes=("udemy.com",),
    )
    assert result.token == "explicit-token"
    assert result.headers == {}


def test_zero_port_drm_override_is_rejected_before_classification() -> None:
    with pytest.raises(LicenseInputError, match="safe HTTPS authority"):
        normalize_widevine_license_input(
            _drm(),
            override_license_url="https://www.udemy.com:0/license",
            extra_headers={"Cookie": "provider=synthetic"},
            trusted_host_suffixes=("udemy.com",),
        )


def test_trailing_dot_drm_override_is_rejected() -> None:
    with pytest.raises(LicenseInputError, match="safe HTTPS authority"):
        normalize_widevine_license_input(
            _drm(),
            override_license_url="https://www.udemy.com./license",
            trusted_host_suffixes=("udemy.com",),
        )


def test_provider_generated_external_license_url_fails_closed() -> None:
    with pytest.raises(LicenseInputError, match="untrusted"):
        normalize_widevine_license_input(
            _drm(license_url="https://license.external.test/path"),
            trusted_host_suffixes=("udemy.com",),
        )


@pytest.mark.asyncio
async def test_license_post_validates_before_request_and_closes_once() -> None:
    client = MagicMock()
    client.post = AsyncMock()
    with (
        patch("rnet.Client", return_value=client),
        pytest.raises(LicensePostError, match="unsafe authority"),
    ):
        await post_license_challenge("https://user@license.external.test/path", b"challenge", {})
    client.post.assert_not_awaited()

    response = _Response(200)
    client.post = AsyncMock(return_value=response)
    with patch("rnet.Client", return_value=client):
        assert (
            await post_license_challenge("https://license.external.test/path", b"challenge", {})
            == b"license"
        )
    assert client.post.await_args.kwargs["allow_redirects"] is False
    response.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_zero_port_license_post_rejects_before_request_or_response() -> None:
    response = _Response(200)
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    with (
        patch("rnet.Client", return_value=client) as client_factory,
        pytest.raises(LicensePostError, match="unsafe authority"),
    ):
        await post_license_challenge("https://license.external.test:0/path", b"challenge", {})

    client_factory.assert_not_called()
    client.post.assert_not_awaited()
    response.bytes.assert_not_awaited()
    response.close.assert_not_awaited()
