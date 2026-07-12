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


async def _poll_auth_cookie(
    ctx: BrowserContext, platform: str, *, interval: float = 2.0
) -> bool:
    """Espera (polling) a que aparezca una cookie de sesión conocida en el contexto."""
    known = _SESSION_COOKIES.get(platform, [])
    while True:
        cookies = await ctx.cookies()
        names = {c["name"] for c in cookies}
        if any(k in names for k in known):
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

        cookies = await ctx.cookies()
        browser.save_cookies(cookies, platform)
        console.print("[green]Sesión guardada correctamente.[/green]")
        return True


async def is_logged_in(platform: str) -> bool:
    """Verifica si hay una sesión válida navegando con las cookies guardadas.

    Si el navegador headless no puede cargar la página (Cloudflare), hace un
    chequeo de respaldo: busca nombres de cookie de sesión conocidos en el
    archivo persistido de la plataforma.
    """
    original_cookies = browser.load_cookies(platform)
    if not original_cookies:
        return False
    extractor = get_extractor_by_name(platform)
    async with browser.browser_context(headless=True, with_session=True, platform=platform) as ctx:
        page = await ctx.new_page()
        await page.goto(extractor.home_url)
        try:
            await page.wait_for_selector(extractor.auth_ready_selector, timeout=8000)
            return True
        except Exception:
            # Fallback: verificar cookies de sesión conocidas (bypass Cloudflare).
            known = _SESSION_COOKIES.get(platform, [])
            if known:
                stored_names = {c.get("name") for c in original_cookies}
                return any(k in stored_names for k in known)
            return False


def logout(platform: str) -> bool:
    """Cierra la sesión de la plataforma eliminando sus cookies persistidas."""
    return browser.clear_session(platform)
