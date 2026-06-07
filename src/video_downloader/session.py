"""Autenticación: login manual asistido por navegador y verificación de sesión."""

from __future__ import annotations

from rich.console import Console

from . import browser
from .config import LOGIN_TIMEOUT_S, LOGIN_URL, PLATZI_BASE_URL

console = Console()


async def login(*, headless: bool = False, timeout_s: int = LOGIN_TIMEOUT_S) -> bool:
    """Abre el navegador para que el usuario inicie sesión manualmente.

    Detecta el login esperando a que el avatar del menú aparezca, y entonces
    persiste las cookies del contexto. Devuelve True si tuvo éxito.

    El login se hace en modo *no headless* aunque ``headless`` sea True, porque
    requiere interacción humana (credenciales, posible 2FA/captcha).
    """
    console.print("[cyan]Abriendo navegador para iniciar sesión en Platzi...[/cyan]")
    console.print(
        f"[yellow]Tienes {timeout_s}s para iniciar sesión manualmente.[/yellow]"
    )

    async with browser.browser_context(headless=False, with_session=False) as ctx:
        page = await ctx.new_page()
        await page.goto(LOGIN_URL)

        # El avatar del menú de usuario solo aparece tras autenticarse.
        try:
            await page.wait_for_selector(
                "[class*='Menu'] img, [class*='Avatar'], a[href*='/p/']",
                timeout=timeout_s * 1000,
            )
        except Exception:
            console.print("[red]No se detectó el inicio de sesión a tiempo.[/red]")
            return False

        cookies = await ctx.cookies()
        browser.save_cookies(cookies)
        console.print("[green]Sesión guardada correctamente.[/green]")
        return True


async def is_logged_in() -> bool:
    """Verifica si hay una sesión válida navegando con las cookies guardadas."""
    if not browser.load_cookies():
        return False
    async with browser.browser_context(headless=True, with_session=True) as ctx:
        page = await ctx.new_page()
        await page.goto(PLATZI_BASE_URL)
        try:
            await page.wait_for_selector(
                "[class*='Menu'] img, [class*='Avatar'], a[href*='/p/']",
                timeout=8000,
            )
            return True
        except Exception:
            return False


def logout() -> bool:
    """Cierra sesión eliminando las cookies persistidas."""
    return browser.clear_session()
