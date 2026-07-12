"""Tests for Widevine license input normalization."""

from __future__ import annotations

import pytest

from evdownloader.drm.license import (
    UDEMY_WIDEVINE_PROXY_URL,
    LicenseInputError,
    build_udemy_widevine_proxy_url,
    normalize_widevine_license_input,
)
from evdownloader.models import DrmInfo

# -- helpers ----------------------------------------------------------------

def _make_drm(**kwargs: object) -> DrmInfo:
    defaults = {
        "scheme": "widevine",
        "pssh": "AAAAV3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADc=",
    }
    defaults.update(kwargs)  # type: ignore[arg-type]
    return DrmInfo(**defaults)  # type: ignore[arg-type]


# -- normalize_widevine_license_input tests --------------------------------


class TestNormalizeSuccess:
    """Provider values produce a valid WidevineLicenseInput."""

    def test_provider_values_only(self) -> None:
        drm = _make_drm(license_url="https://license.example.com")
        result = normalize_widevine_license_input(drm)
        assert result.license_url == "https://license.example.com"
        assert result.pssh == drm.pssh
        assert result.token is None
        assert result.scheme == "widevine"

    def test_token_from_provider(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            token="provider-token-123",
        )
        result = normalize_widevine_license_input(drm)
        assert result.token == "provider-token-123"
        assert result.token_present()

    def test_key_id_propagated(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            key_id="fbf0dce4-2f8b-48b2-9229-1629595c0170",
        )
        result = normalize_widevine_license_input(drm)
        assert result.key_id == "fbf0dce4-2f8b-48b2-9229-1629595c0170"

    def test_headers_propagated(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            headers={"X-Custom": "value"},
        )
        result = normalize_widevine_license_input(drm)
        assert result.headers == {"X-Custom": "value"}


class TestCLIPrecedence:
    """CLI overrides win over provider values."""

    def test_override_license_url_wins(self) -> None:
        drm = _make_drm(license_url="https://provider.com")
        result = normalize_widevine_license_input(
            drm, override_license_url="https://override.com"
        )
        assert result.license_url == "https://override.com"

    def test_override_token_wins(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            token="provider-token",
        )
        result = normalize_widevine_license_input(
            drm, override_token="cli-token"
        )
        assert result.token == "cli-token"

    def test_default_license_url_fallback(self) -> None:
        drm = _make_drm(license_url=None)
        result = normalize_widevine_license_input(
            drm, default_license_url="https://default.com"
        )
        assert result.license_url == "https://default.com"

    def test_override_beats_default(self) -> None:
        drm = _make_drm(license_url=None)
        result = normalize_widevine_license_input(
            drm,
            default_license_url="https://default.com",
            override_license_url="https://override.com",
        )
        assert result.license_url == "https://override.com"

    def test_detected_url_used_when_no_override(self) -> None:
        drm = _make_drm(license_url="https://detected.com")
        result = normalize_widevine_license_input(drm)
        assert result.license_url == "https://detected.com"


class TestHeadersMerge:
    """Headers merge with extras overriding on collision."""

    def test_extras_override_provider(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            headers={"X-A": "provider", "X-B": "only-provider"},
        )
        result = normalize_widevine_license_input(
            drm, extra_headers={"X-A": "extra"}
        )
        assert result.headers["X-A"] == "extra"
        assert result.headers["X-B"] == "only-provider"

    def test_extras_only(self) -> None:
        drm = _make_drm(license_url="https://license.example.com")
        result = normalize_widevine_license_input(
            drm, extra_headers={"X-New": "val"}
        )
        assert result.headers == {"X-New": "val"}


class TestRedactedSummary:
    """safe_summary never leaks token or sensitive headers."""

    def test_token_never_in_summary(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            token="secret-token-123",
        )
        result = normalize_widevine_license_input(drm)
        summary = result.safe_summary()
        assert "secret-token-123" not in summary
        assert "token_present=True" in summary

    def test_no_token_shows_false(self) -> None:
        drm = _make_drm(license_url="https://license.example.com")
        result = normalize_widevine_license_input(drm)
        summary = result.safe_summary()
        assert "token_present=False" in summary

    def test_authorization_header_redacted(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            headers={"Authorization": "Bearer secret", "X-Custom": "visible"},
        )
        result = normalize_widevine_license_input(drm)
        summary = result.safe_summary()
        assert "secret" not in summary
        assert "Authorization=***" in summary
        assert "X-Custom=visible" in summary

    def test_token_like_header_redacted(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            headers={"X-Auth-Token": "secret-token", "X-Custom": "visible"},
        )
        result = normalize_widevine_license_input(drm)
        summary = result.safe_summary()
        assert "secret-token" not in summary
        assert "X-Auth-Token=***" in summary
        assert "X-Custom=visible" in summary

    def test_cookie_header_redacted(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            headers={"Cookie": "session=abc123"},
        )
        result = normalize_widevine_license_input(drm)
        redacted = result.redacted_headers()
        assert redacted["Cookie"] == "***"

    def test_pssh_shown_as_length(self) -> None:
        drm = _make_drm(
            license_url="https://license.example.com",
            pssh="AAAA" * 10,
        )
        result = normalize_widevine_license_input(drm)
        summary = result.safe_summary()
        assert "pssh=<len:40>" in summary

    def test_license_url_auth_token_query_redacted(self) -> None:
        drm = _make_drm(
            license_url=(
                "https://www.udemy.com/media-license-server/validate-auth-token"
                "?drm_type=widevine&auth_token=secret-jwt"
            )
        )
        result = normalize_widevine_license_input(drm)
        summary = result.safe_summary()
        assert "secret-jwt" not in summary
        assert "auth_token=%2A%2A%2A" in summary


class TestValidationErrors:
    """Invalid inputs raise LicenseInputError with actionable messages."""

    def test_wrong_scheme(self) -> None:
        drm = _make_drm(scheme="fairplay", license_url="https://x.com")
        with pytest.raises(LicenseInputError, match="widevine"):
            normalize_widevine_license_input(drm)

    def test_missing_pssh(self) -> None:
        drm = _make_drm(pssh=None, license_url="https://x.com")
        with pytest.raises(LicenseInputError, match="[Pp][Ss][Ss][Hh]"):
            normalize_widevine_license_input(drm)

    def test_missing_license_url(self) -> None:
        drm = _make_drm(license_url=None)
        with pytest.raises(LicenseInputError, match="license URL"):
            normalize_widevine_license_input(drm)

    def test_wrong_scheme_error_includes_got_value(self) -> None:
        drm = _make_drm(scheme="playready", license_url="https://x.com")
        with pytest.raises(LicenseInputError, match="playready"):
            normalize_widevine_license_input(drm)


class TestUdemyProxyUrl:
    """Runtime Udemy proxy URL construction."""

    def test_builds_proxy_url_with_token(self) -> None:
        drm = _make_drm(license_url=UDEMY_WIDEVINE_PROXY_URL, token="secret-jwt")
        license_input = normalize_widevine_license_input(drm)

        result = build_udemy_widevine_proxy_url(license_input)

        assert result == (
            "https://www.udemy.com/media-license-server/validate-auth-token"
            "?drm_type=widevine&auth_token=secret-jwt"
        )

    def test_preserves_unrelated_query_params(self) -> None:
        drm = _make_drm(
            license_url=f"{UDEMY_WIDEVINE_PROXY_URL}?tenant=udemy",
            token="secret-jwt",
        )
        license_input = normalize_widevine_license_input(drm)

        result = build_udemy_widevine_proxy_url(license_input)

        assert result == (
            "https://www.udemy.com/media-license-server/validate-auth-token"
            "?tenant=udemy&drm_type=widevine&auth_token=secret-jwt"
        )

    def test_replaces_existing_drm_params(self) -> None:
        drm = _make_drm(
            license_url=(
                f"{UDEMY_WIDEVINE_PROXY_URL}"
                "?drm_type=playready&auth_token=old&tenant=udemy"
            ),
            token="secret-jwt",
        )
        license_input = normalize_widevine_license_input(drm)

        result = build_udemy_widevine_proxy_url(license_input)

        assert result == (
            "https://www.udemy.com/media-license-server/validate-auth-token"
            "?tenant=udemy&drm_type=widevine&auth_token=secret-jwt"
        )

    def test_requires_token(self) -> None:
        drm = _make_drm(license_url=UDEMY_WIDEVINE_PROXY_URL)
        license_input = normalize_widevine_license_input(drm)

        with pytest.raises(LicenseInputError, match="media license token"):
            build_udemy_widevine_proxy_url(license_input)

    def test_runtime_url_is_redacted_in_safe_summary(self) -> None:
        drm = _make_drm(license_url=UDEMY_WIDEVINE_PROXY_URL, token="secret-jwt")
        license_input = normalize_widevine_license_input(drm)
        runtime_url = build_udemy_widevine_proxy_url(license_input)
        runtime_input = normalize_widevine_license_input(
            _make_drm(license_url=runtime_url, token="secret-jwt")
        )

        summary = runtime_input.safe_summary()

        assert "secret-jwt" not in summary
        assert "auth_token=%2A%2A%2A" in summary
