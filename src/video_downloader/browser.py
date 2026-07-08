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

from .config import DEFAULT_USER_AGENT, ensure_dirs, session_file
from .models import Cookie


def load_cookies(platform: str) -> list[dict[str, Any]]:
    """Lee las cookies persistidas de la plataforma, o lista vacía."""
    path = session_file(platform)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("cookies", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_cookies(cookies: Sequence[Mapping[str, Any]], platform: str) -> None:
    """Persiste las cookies de la plataforma a disco."""
    ensure_dirs()
    session_file(platform).write_text(
        json.dumps({"cookies": list(cookies)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_session(platform: str) -> bool:
    """Elimina el archivo de sesión de la plataforma. Devuelve True si existía."""
    path = session_file(platform)
    if path.exists():
        path.unlink()
        return True
    return False


def cookies_as_dict(cookies: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Convierte cookies de Playwright en un dict ``name -> value``."""
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


def cookies_as_records(cookies: Sequence[Mapping[str, Any]]) -> list[Cookie]:
    """Convierte cookies de Playwright en ``Cookie`` completos (para cookiefile)."""
    records: list[Cookie] = []
    for c in cookies:
        if "name" not in c or "value" not in c:
            continue
        records.append(
            Cookie(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                secure=bool(c.get("secure", False)),
                expires=float(c.get("expires", 0) or 0),
            )
        )
    return records


@asynccontextmanager
async def browser_context(
    *, headless: bool = True, with_session: bool = True, platform: str | None = None
) -> AsyncIterator[BrowserContext]:
    """Abre un contexto de navegador con UA coherente y cookies opcionales.

    Si ``with_session`` es True, carga las cookies de ``platform`` (obligatorio
    en ese caso).

    Uso::

        async with browser_context(platform="platzi") as ctx:
            page = await ctx.new_page()
            ...
    """
    if with_session and platform is None:
        raise ValueError("browser_context requiere 'platform' cuando with_session=True")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
        if with_session and platform is not None:
            cookies = load_cookies(platform)
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
