"""Interfaz de línea de comandos (Typer)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from . import cache, session
from .config import Settings, ensure_dirs

app = typer.Typer(
    name="evdownloader",
    help="Descargador de cursos de video (Platzi, Udemy, Codigofacilito).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_PLATFORM_ARG = typer.Argument("platzi", help="Plataforma: platzi | udemy | codigofacilito.")
_PLATFORMS = ("platzi", "udemy", "codigofacilito")


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
        Path.cwd() / "downloads",
        "-o",
        "--output",
        help="Directorio de salida (se organiza en <output>/<Plataforma>/<curso>).",
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
        help="Navegador del que leer cookies (chrome, brave, safari...). "
        "Requerido para Udemy y Codigofacilito.",
    ),
    sub_langs: str = typer.Option(
        "all", "--sub-langs", help="Idiomas de subtítulos (yt-dlp): all, es,en, es.* ..."
    ),
    use_drm: bool = typer.Option(
        False,
        "--use-drm/--no-use-drm",
        help="Habilitar descifrado DRM si se detecta contenido protegido.",
    ),
    drm_license_server: str | None = typer.Option(
        None,
        "--drm-license-server",
        help="Override de la URL de la license server DRM.",
    ),
    drm_token: str | None = typer.Option(
        None,
        "--drm-token",
        help="Token de autorización para la license server DRM.",
    ),
    drm_device: Path | None = typer.Option(
        None,
        "--drm-device",
        help="Ruta al archivo .wvd del dispositivo Widevine para descifrado DRM.",
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
        sub_langs=sub_langs,
        use_drm=use_drm,
        drm_license_server=drm_license_server,
        drm_token=drm_token,
        drm_device=drm_device,
    )
    asyncio.run(service.download_course(url, settings, use_cache=not no_cache))


@app.command("clear-cache")
def clear_cache() -> None:
    """Borra la caché de estructura de cursos."""
    n = cache.clear()
    console.print(f"[green]Caché borrada ({n} archivos).[/green]")


@app.command()
def drm_proof(
    input: Path = typer.Argument(..., help="Ruta al archivo .mp4 cifrado."),
    output: Path = typer.Argument(..., help="Ruta de salida para el .mp4 descifrado."),
    device: Path = typer.Option(
        ..., "--device", help="Ruta al archivo .wvd del dispositivo Widevine."
    ),
    license_url: str = typer.Option(
        ..., "--license-url", help="URL de la license server Widevine."
    ),
    pssh: str = typer.Option(..., "--pssh", help="PSSH Widevine (base64)."),
    token: str | None = typer.Option(
        None, "--token", help="Token de autorización para la license server DRM."
    ),
    key_id: str | None = typer.Option(None, "--key-id", help="Key ID (hex) del contenido."),
    header: list[str] = typer.Option(
        [],
        "--header",
        help="Headers HTTP adicionales en formato 'Nombre: Valor' (repetible).",
    ),
) -> None:
    """Descifra un archivo MP4 cifrado con Widevine usando un dispositivo .wvd."""
    ensure_dirs()
    from .drm import (
        LicenseInputError,
        ProofError,
        normalize_widevine_license_input,
        prove_decrypt_path,
    )
    from .drm.license import post_license_challenge
    from .models import DrmInfo

    # Parse headers from "Name: Value" format.
    headers: dict[str, str] = {}
    for h in header:
        if ":" not in h:
            console.print(f"[red]Header inválido (falta ':'): {h}[/red]")
            raise typer.Exit(code=1)
        name, value = h.split(":", 1)
        headers[name.strip()] = value.strip()

    # Build DrmInfo for the normalizer.
    drm = DrmInfo(
        scheme="widevine",
        license_url=license_url,
        pssh=pssh,
        token=token,
        key_id=key_id,
        headers=headers,
    )

    try:
        license_input = normalize_widevine_license_input(drm)
    except LicenseInputError as exc:
        console.print(f"[red]Entrada DRM inválida: {exc}[/red]")
        raise typer.Exit(code=1) from None

    # Validate inputs.
    if not input.is_file():
        console.print(f"[red]Archivo de entrada no encontrado: {input}[/red]")
        raise typer.Exit(code=1)
    if not device.is_file():
        console.print(f"[red]Archivo de dispositivo no encontrado: {device}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[cyan]Entrada:[/cyan] {license_input.safe_summary()}")

    try:
        result = asyncio.run(
            prove_decrypt_path(
                license_input=license_input,
                device_path=device,
                encrypted_path=input,
                output_path=output,
                license_post=post_license_challenge,
            )
        )
    except ProofError as exc:
        console.print(f"[red]Fallo en el descifrado DRM: {exc}[/red]")
        raise typer.Exit(code=1) from None

    console.print(f"[green]Descifrado completado: {result.output_path}[/green]")


@app.command()
def status(
    platform: str | None = typer.Argument(
        None, help="Plataforma: platzi | udemy | codigofacilito. Si se omite, muestra todas."
    ),
) -> None:
    """Muestra si hay una sesión activa en una o todas las plataformas."""
    platforms = (platform,) if platform else _PLATFORMS
    for name in platforms:
        logged = asyncio.run(session.is_logged_in(name))
        if logged:
            console.print(f"[green]{name}: sesión activa.[/green]")
        else:
            console.print(
                f"[yellow]{name}: sin sesión. Ejecuta 'evdownloader login {name}'.[/yellow]"
            )


@app.command()
def setup() -> None:
    """Instala el navegador Chromium de Playwright (necesario para login en todas las plataformas).

    Todas las plataformas (Platzi, Udemy, Codigofacilito) usan el navegador
    para obtener la sesión manualmente. Udemy y Codigofacilito además necesitan
    ``--cookies-from-browser`` para descargar (evitan Cloudflare).
    """
    import subprocess
    import sys

    console.print("[cyan]Instalando Chromium de Playwright (necesario para Platzi)…[/cyan]")
    code = subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    if code == 0:
        console.print("[green]Chromium instalado.[/green]")
    else:
        console.print("[red]Falló la instalación de Chromium.[/red]")
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
