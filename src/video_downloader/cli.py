"""Interfaz de línea de comandos (Typer)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from . import cache, session
from .config import Settings, ensure_dirs

app = typer.Typer(
    name="video-downloader",
    help="Descargador de cursos de video (Platzi, Udemy).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_PLATFORM_ARG = typer.Argument("platzi", help="Plataforma: platzi | udemy.")


@app.command()
def login(platform: str = _PLATFORM_ARG) -> None:
    """Inicia sesión en la plataforma (abre el navegador para login manual)."""
    ensure_dirs()
    ok = asyncio.run(session.login(platform))
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def logout(platform: str = _PLATFORM_ARG) -> None:
    """Cierra la sesión de la plataforma eliminando sus cookies guardadas."""
    if session.logout(platform):
        console.print("[green]Sesión cerrada.[/green]")
    else:
        console.print("[yellow]No había una sesión activa.[/yellow]")


@app.command()
def download(
    url: str = typer.Argument(..., help="URL del curso a descargar."),
    quality: str | None = typer.Option(
        None, "-q", "--quality", help="Calidad máxima: 1080, 720... (def: máxima)."
    ),
    output: Path = typer.Option(
        Path.cwd() / "Courses", "-o", "--output", help="Directorio de salida."
    ),
    downloader: str = typer.Option(
        "ytdlp", "-d", "--downloader", help="Motor: ytdlp (def) o native."
    ),
    overwrite: bool = typer.Option(
        False, "-w", "--overwrite", help="Sobrescribir archivos existentes."
    ),
    limit: int | None = typer.Option(
        None, "-n", "--limit", help="Descargar solo las primeras N clases de video."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Ignorar la caché de estructura del curso."
    ),
    no_resources: bool = typer.Option(
        False, "--no-resources", help="No descargar resumen, adjuntos, enlaces ni MHTML."
    ),
    cookies_from_browser: str | None = typer.Option(
        None,
        "--cookies-from-browser",
        help="Navegador del que leer cookies (chrome, brave, safari...). Requerido para Udemy.",
    ),
    show_browser: bool = typer.Option(
        False, "--show-browser", help="Mostrar el navegador (no headless)."
    ),
) -> None:
    """Descarga un curso completo."""
    ensure_dirs()
    from . import service  # import diferido (carga Playwright/yt-dlp)

    settings = Settings(
        download_dir=output,
        quality=quality,
        overwrite=overwrite,
        downloader=downloader,
        headless=not show_browser,
        limit=limit,
        resources=not no_resources,
        cookies_from_browser=cookies_from_browser,
    )
    asyncio.run(service.download_course(url, settings, use_cache=not no_cache))


@app.command("clear-cache")
def clear_cache() -> None:
    """Borra la caché de estructura de cursos."""
    n = cache.clear()
    console.print(f"[green]Caché borrada ({n} archivos).[/green]")


@app.command()
def status(platform: str = _PLATFORM_ARG) -> None:
    """Muestra si hay una sesión activa en la plataforma."""
    logged = asyncio.run(session.is_logged_in(platform))
    if logged:
        console.print("[green]Sesión activa.[/green]")
    else:
        console.print(
            f"[yellow]Sin sesión. Ejecuta 'video-downloader login {platform}'.[/yellow]"
        )


if __name__ == "__main__":
    app()
