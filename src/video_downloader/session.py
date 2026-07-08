"""Autenticación: login manual asistido por navegador y verificación de sesión.

Es agnóstico a la plataforma: toma la URL de login, la URL de verificación y el
selector que delata la sesión activa del propio extractor (``login_url``,
``home_url``, ``auth_ready_selector``). Cada plataforma persiste sus cookies por
separado (``session-{platform}.json``).
"""

from __future__ import annotations

from rich.console import Console

from . import browser
from .config import LOGIN_TIMEOUT_S
from .extractors import get_extractor_by_name

console = Console()


async def login(platform: str, *, timeout_s: int = LOGIN_TIMEOUT_S) -> bool:
    """Abre el navegador para que el usuario inicie sesión manualmente.

    Detecta el login esperando a que aparezca el selector de sesión activa de la
    plataforma, y entonces persiste las cookies del contexto. Devuelve True si
    tuvo éxito.

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

        try:
            await page.wait_for_selector(
                extractor.auth_ready_selector,
                timeout=timeout_s * 1000,
            )
        except Exception:
            console.print("[red]No se detectó el inicio de sesión a tiempo.[/red]")
            return False

        cookies = await ctx.cookies()
        browser.save_cookies(cookies, platform)
        console.print("[green]Sesión guardada correctamente.[/green]")
        return True


async def is_logged_in(platform: str) -> bool:
    """Verifica si hay una sesión válida navegando con las cookies guardadas."""
    if not browser.load_cookies(platform):
        return False
    extractor = get_extractor_by_name(platform)
    async with browser.browser_context(headless=True, with_session=True, platform=platform) as ctx:
        page = await ctx.new_page()
        await page.goto(extractor.home_url)
        try:
            await page.wait_for_selector(extractor.auth_ready_selector, timeout=8000)
            return True
        except Exception:
            return False


def logout(platform: str) -> bool:
    """Cierra la sesión de la plataforma eliminando sus cookies persistidas."""
    return browser.clear_session(platform)
