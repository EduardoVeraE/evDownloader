"""Gestión del navegador Playwright y persistencia de sesión (cookies).

Centraliza la creación del contexto de navegación con identidad coherente
(mismo User-Agent que luego usa el downloader) y la carga/guardado de cookies.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import DEFAULT_USER_AGENT, SESSION_FILE, ensure_dirs


def load_cookies() -> list[dict[str, Any]]:
    """Lee las cookies persistidas de la sesión, o lista vacía."""
    if not SESSION_FILE.exists():
        return []
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return data.get("cookies", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_cookies(cookies: Sequence[Mapping[str, Any]]) -> None:
    """Persiste las cookies de la sesión a disco."""
    ensure_dirs()
    SESSION_FILE.write_text(
        json.dumps({"cookies": list(cookies)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_session() -> bool:
    """Elimina el archivo de sesión. Devuelve True si existía."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        return True
    return False


def cookies_as_dict(cookies: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Convierte cookies de Playwright en un dict ``name -> value``."""
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


@asynccontextmanager
async def browser_context(
    *, headless: bool = True, with_session: bool = True
) -> AsyncIterator[BrowserContext]:
    """Abre un contexto de navegador con UA coherente y cookies opcionales.

    Uso::

        async with browser_context() as ctx:
            page = await ctx.new_page()
            ...
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
        if with_session:
            cookies = load_cookies()
            if cookies:
                await context.add_cookies(cast("Any", cookies))
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


async def new_page(context: BrowserContext) -> Page:
    """Crea una página nueva en el contexto dado."""
    return await context.new_page()
