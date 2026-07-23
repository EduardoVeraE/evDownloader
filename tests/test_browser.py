"""Pruebas de resolución y persistencia local de cookies."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from evdownloader import browser
from evdownloader.models import Cookie


def test_cookie_header_matches_leading_dot_domain_exact_and_subdomain() -> None:
    cookies = [Cookie(name="session", value="yes", domain=".example.com")]

    assert browser.cookie_header_for_url(cookies, "https://example.com", now=100) == "session=yes"
    assert (
        browser.cookie_header_for_url(cookies, "https://api.example.com/path", now=100)
        == "session=yes"
    )
    assert browser.cookie_header_for_url(cookies, "https://badexample.com", now=100) is None


def test_cookie_header_treats_undotted_domain_as_host_only() -> None:
    cookies = [Cookie(name="host", value="only", domain="example.com")]

    assert browser.cookie_header_for_url(cookies, "https://example.com", now=100) == "host=only"
    assert browser.cookie_header_for_url(cookies, "https://www.example.com", now=100) is None


def test_cookie_header_matches_ip_addresses_exactly() -> None:
    cookies = [
        Cookie(name="exact", value="yes", domain=".127.0.0.1"),
        Cookie(name="suffix", value="no", domain=".0.0.1"),
    ]

    assert browser.cookie_header_for_url(cookies, "http://127.0.0.1", now=100) == "exact=yes"


def test_cookie_header_applies_path_boundary_order_and_preserves_duplicates() -> None:
    cookies = [
        Cookie(name="id", value="root", domain="example.com", path="/"),
        Cookie(name="id", value="first", domain="example.com", path="/account"),
        Cookie(name="theme", value="dark", domain="example.com", path="/account"),
        Cookie(name="id", value="deep", domain="example.com", path="/account/settings"),
        Cookie(name="attack", value="no", domain="example.com", path="/accounting"),
    ]

    assert (
        browser.cookie_header_for_url(
            cookies, "https://example.com/account/settings/profile", now=100
        )
        == "id=deep; id=first; theme=dark; id=root"
    )


def test_cookie_header_keeps_common_safe_names_and_values() -> None:
    cookies = [
        Cookie(name="__Host-session.v2", value="abc==.%_-~:/?&+", domain="example.com"),
        Cookie(name="empty", value="", domain="example.com"),
    ]

    assert browser.cookie_header_for_url(cookies, "https://example.com", now=100) == (
        "__Host-session.v2=abc==.%_-~:/?&+; empty="
    )


def test_cookie_header_skips_malformed_names_and_injectable_values() -> None:
    cookies = [
        Cookie(name="valid", value="still-safe%3Dyes", domain="example.com"),
        Cookie(name="", value="empty-name", domain="example.com"),
        Cookie(name="bad name", value="space", domain="example.com"),
        Cookie(name="bad=name", value="separator", domain="example.com"),
        Cookie(name="bad;name", value="separator", domain="example.com"),
        Cookie(name="bad\r\nInjected", value="name", domain="example.com"),
        Cookie(name="bad\u00e9", value="non-ascii-name", domain="example.com"),
        Cookie(name="crlf", value="safe\r\nInjected=yes", domain="example.com"),
        Cookie(name="semicolon", value="safe; injected=yes", domain="example.com"),
        Cookie(name="comma", value="safe,injected=yes", domain="example.com"),
        Cookie(name="quote", value='safe"injected', domain="example.com"),
        Cookie(name="backslash", value="safe\\injected", domain="example.com"),
        Cookie(name="space", value="safe injected=yes", domain="example.com"),
        Cookie(name="control", value="safe\x00injected", domain="example.com"),
        Cookie(name="del", value="safe\x7finjected", domain="example.com"),
        Cookie(name="non-ascii", value="safe\u00e9", domain="example.com"),
        Cookie.model_construct(name=None, value="bad", domain="example.com"),
        Cookie.model_construct(name="bad-type", value=None, domain="example.com"),
    ]

    header = browser.cookie_header_for_url(cookies, "https://example.com", now=100)

    assert header == "valid=still-safe%3Dyes"
    assert "Injected" not in header
    assert "injected" not in header
    assert "\r" not in header and "\n" not in header


def test_cookie_header_requires_path_label_boundary() -> None:
    cookies = [Cookie(name="account", value="yes", domain="example.com", path="/account")]

    assert browser.cookie_header_for_url(cookies, "https://example.com/accounting", now=100) is None


def test_cookie_header_sends_secure_cookie_only_over_https() -> None:
    cookies = [Cookie(name="secure", value="yes", domain="example.com", secure=True)]

    assert browser.cookie_header_for_url(cookies, "http://example.com", now=100) is None
    assert browser.cookie_header_for_url(cookies, "https://example.com", now=100) == "secure=yes"


def test_cookie_header_enforces_finite_expiry_and_keeps_session_cookies() -> None:
    cookies = [
        Cookie(name="future", value="yes", domain="example.com", expires=101),
        Cookie(name="zero", value="session", domain="example.com", expires=0),
        Cookie(name="negative", value="session", domain="example.com", expires=-1),
        Cookie(name="now", value="no", domain="example.com", expires=100),
        Cookie(name="past", value="no", domain="example.com", expires=99),
        Cookie(name="nan", value="no", domain="example.com", expires=float("nan")),
        Cookie(name="inf", value="no", domain="example.com", expires=float("inf")),
        Cookie(name="negative_inf", value="no", domain="example.com", expires=float("-inf")),
    ]

    assert browser.cookie_header_for_url(cookies, "https://example.com", now=100) == (
        "future=yes; zero=session; negative=session"
    )


def test_cookie_header_uses_browser_time_when_now_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(browser.time, "time", lambda: 100.0)

    assert (
        browser.cookie_header_for_url(
            [Cookie(name="future", value="yes", domain="example.com", expires=101)],
            "https://example.com",
        )
        == "future=yes"
    )


@pytest.mark.parametrize(
    "domain",
    ["", ".", "..example.com", "example.com.", "exa mple.com", "-example.com", "example.com:443"],
)
def test_cookie_header_rejects_malformed_domain_scopes(domain: str) -> None:
    cookie = Cookie(name="bad", value="no", domain=domain)

    assert browser.cookie_header_for_url([cookie], "https://example.com", now=100) is None


@pytest.mark.parametrize("path", ["", "account", "/bad;path", "/bad\npath"])
def test_cookie_header_rejects_malformed_path_scopes(path: str) -> None:
    cookie = Cookie(name="bad", value="no", domain="example.com", path=path)

    assert browser.cookie_header_for_url([cookie], "https://example.com/account", now=100) is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "example.com/file",
        "https:///missing-host",
        "https://example.com:bad/path",
        "https://[not-ipv6]/path",
        "https://exa mple.com/path",
        "https://example.com/bad\\path",
    ],
)
def test_cookie_header_rejects_unsupported_or_malformed_urls(url: str) -> None:
    cookie = Cookie(name="session", value="yes", domain="example.com")

    assert browser.cookie_header_for_url([cookie], url, now=100) is None


def test_cookie_header_returns_none_when_nothing_matches() -> None:
    cookie = Cookie(name="other", value="no", domain="other.example")

    assert browser.cookie_header_for_url([cookie], "https://example.com", now=100) is None


def test_resolve_cookies_prefiere_sesion_persistida(monkeypatch) -> None:
    persisted = [
        {"name": "access_token", "value": "persisted", "domain": ".udemy.com"},
        {"name": "sid", "value": "third-party", "domain": ".google.com"},
    ]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: persisted)
    monkeypatch.setattr(
        browser,
        "load_browser_cookies",
        lambda browser_name: (_ for _ in ()).throw(AssertionError("no debe usar navegador")),
    )

    assert browser.resolve_cookies("udemy", "brave") == persisted[:1]


def test_resolve_cookies_hace_fallback_explicito_al_navegador(monkeypatch) -> None:
    browser_cookies = [{"name": "access_token", "value": "browser", "domain": ".udemy.com"}]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: [])
    monkeypatch.setattr(browser, "load_browser_cookies", lambda browser_name: browser_cookies)

    assert browser.resolve_cookies("udemy", "brave") == browser_cookies


def test_resolve_cookies_hace_fallback_si_la_sesion_no_es_utilizable(monkeypatch) -> None:
    persisted = [{"name": "access_token", "value": "", "domain": ".udemy.com"}]
    browser_cookies = [{"name": "access_token", "value": "browser", "domain": ".udemy.com"}]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: persisted)
    monkeypatch.setattr(browser, "load_browser_cookies", lambda browser_name: browser_cookies)

    assert browser.resolve_cookies("udemy", "brave") == browser_cookies


def test_resolve_cookies_rechaza_fallback_sin_token_utilizable(monkeypatch) -> None:
    browser_cookies = [{"name": "sid", "value": "browser", "domain": ".udemy.com"}]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: [])
    monkeypatch.setattr(browser, "load_browser_cookies", lambda browser_name: browser_cookies)

    assert browser.resolve_cookies("udemy", "brave") == []


def test_resolve_cookies_fallback_solo_conserva_dominios_udemy(monkeypatch) -> None:
    browser_cookies = [
        {"name": "access_token", "value": "valid", "domain": ".udemy.com"},
        {"name": "sid", "value": "valid", "domain": "www.udemy.com"},
        {"name": "other", "value": "secret", "domain": ".example.com"},
    ]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: [])
    monkeypatch.setattr(browser, "load_browser_cookies", lambda browser_name: browser_cookies)

    resolved = browser.resolve_cookies("udemy", "brave")

    assert resolved == browser_cookies[:2]


def test_udemy_cookie_expirada_no_es_utilizable(monkeypatch) -> None:
    monkeypatch.setattr(browser.time, "time", lambda: 1_000.0)
    assert not browser.has_usable_session(
        "udemy",
        [{"name": "access_token", "value": "token", "domain": ".udemy.com", "expires": 999}],
    )


def test_udemy_rechaza_access_token_de_otro_dominio() -> None:
    assert not browser.has_usable_session(
        "udemy",
        [{"name": "access_token", "value": "token", "domain": ".example.com"}],
    )


def test_cookie_udemy_sin_expiracion_o_con_cero_es_utilizable() -> None:
    assert browser.has_usable_session(
        "udemy", [{"name": "access_token", "value": "token", "domain": "udemy.com"}]
    )
    assert browser.has_usable_session(
        "udemy",
        [{"name": "access_token", "value": "token", "domain": "www.udemy.com", "expires": 0}],
    )


def test_save_cookies_es_atomico_y_restrictivo(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "session-udemy.json"
    monkeypatch.setattr(browser, "session_file", lambda platform: path)
    monkeypatch.setattr(browser, "ensure_dirs", lambda: None)

    browser.save_cookies(
        [
            {"name": "access_token", "value": "not-for-output", "domain": ".udemy.com"},
            {"name": "sid", "value": "third-party", "domain": ".example.com"},
        ],
        "udemy",
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["cookies"] == [
        {"name": "access_token", "value": "not-for-output", "domain": ".udemy.com"}
    ]
    assert not list(tmp_path.glob(".*.tmp"))


def test_load_cookies_restringe_archivo_existente(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "session-udemy.json"
    path.write_text('{"cookies": []}', encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(browser, "session_file", lambda platform: path)

    browser.load_cookies("udemy")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
