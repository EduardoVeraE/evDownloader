"""Pruebas de resolución y persistencia local de cookies."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from evdownloader import browser


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
    browser_cookies = [
        {"name": "access_token", "value": "browser", "domain": ".udemy.com"}
    ]
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
