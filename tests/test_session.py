"""Pruebas deterministas de la comprobación local de sesión."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from evdownloader import browser, session


def test_is_logged_in_rechaza_token_udemy_vacio(monkeypatch) -> None:
    monkeypatch.setattr(
        browser,
        "load_cookies",
        lambda platform: [{"name": "access_token", "value": "", "domain": ".udemy.com"}],
    )

    assert asyncio.run(session.is_logged_in("udemy")) is False


def test_is_logged_in_rechaza_token_udemy_expirado(monkeypatch) -> None:
    monkeypatch.setattr(browser.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        browser,
        "load_cookies",
        lambda platform: [
            {
                "name": "access_token",
                "value": "token",
                "domain": ".udemy.com",
                "expires": 999,
            }
        ],
    )

    assert asyncio.run(session.is_logged_in("udemy")) is False


def test_is_logged_in_usa_cookie_local_si_playwright_no_detecta_sesion(monkeypatch) -> None:
    cookies = [
        {"name": "access_token", "value": "token", "domain": ".udemy.com", "expires": 0}
    ]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: cookies)
    monkeypatch.setattr(
        session,
        "get_extractor_by_name",
        lambda platform: SimpleNamespace(
            home_url="https://example.test/", auth_ready_selector=".logged-in"
        ),
    )

    class FakePage:
        url = "https://example.test/"

        async def goto(self, url: str) -> None:
            return None

        async def wait_for_selector(self, selector: str, timeout: int) -> None:
            raise RuntimeError("selector no disponible")

    class FakeContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def new_page(self) -> FakePage:
            return FakePage()

    monkeypatch.setattr(browser, "browser_context", lambda **kwargs: FakeContext())

    assert asyncio.run(session.is_logged_in("udemy")) is True


def test_poll_auth_cookie_ignora_valor_vacio_y_expirado(monkeypatch) -> None:
    monkeypatch.setattr(browser.time, "time", lambda: 1_000.0)

    class FakeContext:
        def __init__(self) -> None:
            self.calls = 0

        async def cookies(self) -> list[dict[str, object]]:
            self.calls += 1
            values = [
                [{"name": "access_token", "value": "third-party", "domain": ".google.com"}],
                [{"name": "access_token", "value": "", "domain": ".udemy.com"}],
                [
                    {
                        "name": "access_token",
                        "value": "token",
                        "domain": ".udemy.com",
                        "expires": 999,
                    }
                ],
                [
                    {
                        "name": "access_token",
                        "value": "token",
                        "domain": ".udemy.com",
                        "expires": 2_000,
                    }
                ],
            ]
            return values[self.calls - 1]

    ctx = FakeContext()
    assert asyncio.run(session._poll_auth_cookie(ctx, "udemy", interval=0)) is True
    assert ctx.calls == 4


def test_login_no_guarda_cookie_udemy_invalida(monkeypatch) -> None:
    monkeypatch.setattr(
        session,
        "get_extractor_by_name",
        lambda platform: SimpleNamespace(
            login_url="https://www.udemy.com/login", auth_ready_selector=".logged-in"
        ),
    )

    async def detected(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(session, "_poll_login_redirect", detected)
    monkeypatch.setattr(session, "_poll_auth_cookie", detected)

    saved: list[object] = []
    monkeypatch.setattr(browser, "save_cookies", lambda cookies, platform: saved.append(cookies))

    class FakePage:
        async def goto(self, url: str) -> None:
            return None

        async def wait_for_selector(self, selector: str, timeout: int) -> None:
            raise RuntimeError("selector no disponible")

    class FakeContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def new_page(self) -> FakePage:
            return FakePage()

        async def cookies(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "access_token",
                    "value": "",
                    "domain": ".udemy.com",
                    "expires": 2_000,
                }
            ]

    monkeypatch.setattr(browser, "browser_context", lambda **kwargs: FakeContext())

    assert asyncio.run(session.login("udemy", timeout_s=1)) is False
    assert saved == []


def test_login_udemy_solo_guarda_cookies_de_udemy(monkeypatch) -> None:
    monkeypatch.setattr(
        session,
        "get_extractor_by_name",
        lambda platform: SimpleNamespace(
            login_url="https://www.udemy.com/login", auth_ready_selector=".logged-in"
        ),
    )

    async def detected(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(session, "_poll_login_redirect", detected)
    monkeypatch.setattr(session, "_poll_auth_cookie", detected)

    saved: list[list[dict[str, object]]] = []
    monkeypatch.setattr(browser, "save_cookies", lambda cookies, platform: saved.append(cookies))

    class FakePage:
        async def goto(self, url: str) -> None:
            return None

        async def wait_for_selector(self, selector: str, timeout: int) -> None:
            raise RuntimeError("selector no disponible")

    class FakeContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def new_page(self) -> FakePage:
            return FakePage()

        async def cookies(self) -> list[dict[str, object]]:
            return [
                {"name": "access_token", "value": "token", "domain": ".udemy.com"},
                {"name": "access_token", "value": "third-party", "domain": ".google.com"},
                {"name": "sid", "value": "third-party", "domain": ".example.com"},
            ]

    monkeypatch.setattr(browser, "browser_context", lambda **kwargs: FakeContext())

    assert asyncio.run(session.login("udemy", timeout_s=1)) is True
    assert saved == [
        [{"name": "access_token", "value": "token", "domain": ".udemy.com"}]
    ]


def test_is_logged_in_platzi_no_acepta_cookie_generica_si_falla_selector(monkeypatch) -> None:
    cookies = [{"name": "generic", "value": "valid"}]
    monkeypatch.setattr(browser, "load_cookies", lambda platform: cookies)
    monkeypatch.setattr(
        session,
        "get_extractor_by_name",
        lambda platform: SimpleNamespace(
            home_url="https://platzi.test/", auth_ready_selector=".logged-in"
        ),
    )

    class FakePage:
        async def goto(self, url: str) -> None:
            return None

        async def wait_for_selector(self, selector: str, timeout: int) -> None:
            raise RuntimeError("selector no disponible")

    class FakeContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def new_page(self) -> FakePage:
            return FakePage()

    monkeypatch.setattr(browser, "browser_context", lambda **kwargs: FakeContext())

    assert asyncio.run(session.is_logged_in("platzi")) is False
