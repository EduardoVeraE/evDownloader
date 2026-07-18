"""Autenticación: login manual asistido por navegador y verificación de sesión.

Es agnóstico a la plataforma: toma la URL de login, la URL de verificación y el
selector que delata la sesión activa del propio extractor (``login_url``,
``home_url``, ``auth_ready_selector``). Cada plataforma persiste sus cookies por
separado (``session-{platform}.json``).

Algunas plataformas (Udemy) bloquean el navegador headless con Cloudflare
Turnstile. En ese caso ``is_logged_in`` hace una verificación de respaldo:
busca nombres de cookie de sesión conocidos en el archivo persistido.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page
from rich.console import Console

from . import browser
from .config import LOGIN_TIMEOUT_S
from .extractors import get_extractor_by_name

console = Console()

# Nombres de cookie que indican sesión activa en cada plataforma, usados como
# respaldo cuando el navegador headless no puede cargar la página (Cloudflare).
_SESSION_COOKIES: dict[str, list[str]] = {
    "udemy": ["access_token"],
    "codigofacilito": ["_session_id", "_codigofacilito_session"],
}


def _has_valid_auth_cookie(
    platform: str, cookies: Sequence[Mapping[str, Any]]
) -> bool:
    return browser.has_usable_session(platform, cookies)


async def _poll_auth_cookie(
    ctx: BrowserContext, platform: str, *, interval: float = 2.0
) -> bool:
    """Espera (polling) a que aparezca una cookie de sesión conocida en el contexto."""
    while True:
        cookies = await ctx.cookies()
        if _has_valid_auth_cookie(platform, cookies):
            return True
        await asyncio.sleep(interval)


async def _poll_login_redirect(page: Page, *, interval: float = 1.0) -> bool:
    """Detecta que el login terminó cuando la página sale de rutas de login."""
    login_paths = ("/login", "/users/sign_in", "/join/login-popup")
    while True:
        path = urlsplit(page.url).path.rstrip("/")
        if path and not any(p in path for p in login_paths):
            return True
        await asyncio.sleep(interval)


async def login(platform: str, *, timeout_s: int = LOGIN_TIMEOUT_S) -> bool:
    """Abre el navegador para que el usuario inicie sesión manualmente.

    Detecta el login esperando a que aparezca el selector de sesión activa de la
    plataforma O una cookie de sesión conocida, lo que ocurra primero. Entonces
    persiste las cookies del contexto. Devuelve True si tuvo éxito.

    El login se hace siempre en modo *no headless* porque requiere interacción
    humana (credenciales, posible 2FA/captcha).
    """
    extractor = get_extractor_by_name(platform)
    console.print(f"[cyan]Abriendo navegador para iniciar sesión en {platform}...[/cyan]")
    console.print(
        f"[yellow]Tienes {timeout_s}s para iniciar sesión manualmente.[/yellow]"
    )

    async with browser.browser_context(headless=False, with_session=False) as ctx:
        page = await ctx.new_page()
        await page.goto(extractor.login_url)

        tasks = [
            asyncio.create_task(
                page.wait_for_selector(
                    extractor.auth_ready_selector,
                    timeout=timeout_s * 1000,
                )
            ),
            asyncio.create_task(_poll_login_redirect(page)),
        ]
        if _SESSION_COOKIES.get(platform):
            tasks.append(asyncio.create_task(_poll_auth_cookie(ctx, platform)))
        done, pending = await asyncio.wait(
            tasks, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        detected = False
        for t in done:
            try:
                if t.result():
                    detected = True
                    break
            except Exception:
                continue

        if not detected:
            console.print("[red]No se detectó el inicio de sesión a tiempo.[/red]")
            return False

        cookies = browser.filter_cookies(platform, await ctx.cookies())
        if _SESSION_COOKIES.get(platform) and not _has_valid_auth_cookie(platform, cookies):
            console.print(
                "[red]El inicio de sesión no produjo una cookie válida. "
                "Vuelve a intentarlo.[/red]"
            )
            return False
        browser.save_cookies(cookies, platform)
        console.print("[green]Sesión guardada correctamente.[/green]")
        return True


async def is_logged_in(platform: str) -> bool:
    """Verifica si hay una sesión válida navegando con las cookies guardadas.

    Si el navegador headless no puede cargar la página (Cloudflare), hace un
    chequeo de respaldo local de la cookie de sesión persistida. No realiza una
    llamada adicional a la plataforma.
    """
    original_cookies = browser.load_cookies(platform)
    if platform == "platzi":
        if not original_cookies:
            return False
    elif not browser.has_usable_session(platform, original_cookies):
        return False
    extractor = get_extractor_by_name(platform)
    async with browser.browser_context(headless=True, with_session=True, platform=platform) as ctx:
        page = await ctx.new_page()
        await page.goto(extractor.home_url)
        try:
            await page.wait_for_selector(extractor.auth_ready_selector, timeout=8000)
            return True
        except Exception:
            # Fallback local: evita otro endpoint cuando Cloudflare bloquea Playwright.
            if platform in _SESSION_COOKIES:
                return browser.has_usable_session(platform, original_cookies)
            return False


def logout(platform: str) -> bool:
    """Cierra la sesión de la plataforma eliminando sus cookies persistidas."""
    return browser.clear_session(platform)
