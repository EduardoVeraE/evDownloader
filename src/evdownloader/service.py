"""Orquestación de la descarga de un curso completo.

Une extractor (estructura + resolución de video por interceptación) con el
motor de descarga elegido, y organiza la salida en carpetas jerárquicas.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiofiles
import rnet
from rich.console import Console

from . import browser, cache
from .config import RNET_IMPERSONATE, Settings
from .downloaders import get_downloader
from .extractors import get_extractor
from .models import Course, Resource, ResourceKind, Subtitle, Unit, UnitExtras, UnitType
from .utils import numbered, safe_mkdir, slugify

console = Console()

# Extensión "limpia" al final de un nombre (p. ej. ".pdf", ".rar"): distingue un
# título que ya es un nombre de archivo de uno descriptivo con puntos.
_CLEAN_EXT_RE = re.compile(r"^\.[a-zA-Z0-9]{1,5}$")


async def download_course(url: str, settings: Settings, *, use_cache: bool = True) -> None:
    """Descarga un curso completo a ``settings.download_dir``."""
    extractor = get_extractor(url)
    extractor.configure(settings)
    downloader = get_downloader(settings.downloader)

    if extractor.needs_browser:
        async with browser.browser_context(
            headless=settings.headless, platform=extractor.name
        ) as ctx:
            await _run_download(extractor, downloader, ctx, url, settings, use_cache=use_cache)
    else:
        # Extractores que delegan en yt-dlp (Udemy) no abren navegador.
        await _run_download(extractor, downloader, None, url, settings, use_cache=use_cache)

    console.print("[bold green]Descarga finalizada.[/bold green]")


async def _run_download(
    extractor, downloader, ctx, url: str, settings: Settings, *, use_cache: bool
) -> None:
    course = await _load_structure(extractor, ctx, url, use_cache=use_cache)
    console.print(
        f"[bold cyan]{course.title}[/bold cyan] — "
        f"{sum(len(c.units) for c in course.chapters)} unidades en "
        f"{len(course.chapters)} capítulos"
    )

    # Organizar por plataforma: <download_dir>/<Plataforma>/<curso>.
    platform_dir = extractor.name.capitalize()
    course_dir = safe_mkdir(settings.download_dir / platform_dir / slugify(course.title))

    downloaded = 0
    for chapter in course.chapters:
        chapter_dir = safe_mkdir(course_dir / numbered(chapter.index, chapter.title))
        for unit in chapter.units:
            if unit.type == UnitType.VIDEO:
                if settings.limit is not None and downloaded >= settings.limit:
                    console.print(f"[dim]Límite de {settings.limit} clases alcanzado.[/dim]")
                    break
                downloaded += 1
            await _process_unit(extractor, downloader, ctx, unit, chapter_dir, settings)
        else:
            continue
        break


async def _load_structure(extractor, ctx, url: str, *, use_cache: bool) -> Course:
    if use_cache:
        cached = cache.get(url)
        if cached:
            console.print("[dim]Estructura cargada desde caché.[/dim]")
            return Course.model_validate(cached)
    course = await extractor.list_course(ctx, url)
    cache.set(url, course.model_dump())
    return course


async def _process_unit(
    extractor, downloader, ctx, unit: Unit, out_dir: Path, settings: Settings
) -> None:
    base = out_dir / numbered(unit.index, unit.title)
    if unit.type == UnitType.VIDEO:
        await _download_video(extractor, downloader, ctx, unit, base, settings)
    else:
        console.print(f"[dim]{unit.type.value}: {unit.title}[/dim]")
    if settings.resources:
        await _save_extras(extractor, ctx, unit, base, settings)


async def _download_video(
    extractor, downloader, ctx, unit: Unit, base: Path, settings: Settings
) -> None:
    final = base.with_suffix(".mp4")
    if final.exists() and not settings.overwrite:
        console.print(f"[dim]Ya existe: {final.name}[/dim]")
        return

    console.print(f"[cyan]Resolviendo:[/cyan] {unit.title}")
    source = await extractor.resolve_video(ctx, unit)
    if source is None:
        console.print(f"[red]Sin fuente de video para:[/red] {unit.title}")
        return

    try:
        path = await downloader.download(source, base, settings)
        console.print(f"[green]✓[/green] {path.name}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Falló la descarga de {unit.title}: {exc}[/red]")
        return

    await _save_subtitles(source.subtitles, base, source)


async def _save_extras(
    extractor, ctx, unit: Unit, base: Path, settings: Settings
) -> None:
    """Guarda resumen, archivos adjuntos, enlaces y, si aplica, el MHTML."""
    # Para lectures/quizzes (sin video) capturamos la página completa como MHTML.
    capture = unit.type is not UnitType.VIDEO
    extras: UnitExtras = await extractor.resolve_extras(ctx, unit, capture_page=capture)

    if extras.summary_html:
        await _write_text(
            base.with_suffix(".resumen.html"), _wrap_html(unit.title, extras.summary_html)
        )

    if extras.page_mhtml:
        await _write_text(base.with_suffix(".mhtml"), extras.page_mhtml)

    files = [r for r in extras.resources if r.kind is ResourceKind.FILE]
    links = [r for r in extras.resources if r.kind is ResourceKind.LINK]

    if links:
        body = "\n".join(f"- [{lk.title}]({lk.url})" for lk in links)
        await _write_text(base.with_suffix(".enlaces.md"), f"# Enlaces — {unit.title}\n\n{body}\n")

    if files:
        # Udemy no abre navegador (ctx=None); sus URLs de recurso vienen firmadas.
        cookies = browser.cookies_as_dict(await ctx.cookies()) if ctx is not None else {}
        res_dir = safe_mkdir(base.parent / (base.name + "-recursos"))
        await _download_files(files, res_dir, cookies)


async def _save_subtitles(subtitles: list[Subtitle], base: Path, source) -> None:
    if not subtitles:
        return
    client = rnet.Client(impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None))
    headers = dict(source.http_headers)
    if source.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in source.cookies.items())
    for sub in subtitles:
        try:
            resp = await client.get(sub.url, headers=headers)
            content = await resp.text()
            sub_path = base.with_suffix(f".{sub.lang}.vtt")
            async with aiofiles.open(sub_path, "w", encoding="utf-8") as f:
                await f.write(content)
        except Exception:  # noqa: BLE001, S112
            continue


async def _download_files(
    files: list[Resource], out_dir: Path, cookies: dict[str, str]
) -> None:
    """Descarga los archivos adjuntos a ``out_dir`` con la sesión activa."""
    client = rnet.Client(impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None))
    headers: dict[str, str] = {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    for res in files:
        name = _filename_from_url(res.url, res.title)
        try:
            resp = await client.get(res.url, headers=headers)
            data = await resp.bytes()
            async with aiofiles.open(out_dir / name, "wb") as f:
                await f.write(data)
            console.print(f"  [green]·[/green] recurso: {name}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]·[/red] falló recurso {name}: {exc}")


def _filename_from_url(url: str, title: str) -> str:
    """Deriva un nombre de archivo seguro de la URL (o del título de respaldo)."""
    # Un título que ya es un nombre de archivo (extensión limpia al final) es más
    # fiable que el nombre de la URL: las descargas firmadas de Udemy terminan
    # todas en "original.<ext>".
    if title and _CLEAN_EXT_RE.match(Path(title).suffix):
        return slugify(title)
    path = unquote(urlsplit(url).path)
    candidate = Path(path).name
    if candidate and "." in candidate:
        return slugify(candidate)
    return slugify(title)


async def _write_text(path: Path, content: str) -> None:
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)


def _wrap_html(title: str, body_html: str) -> str:
    """Envuelve el HTML del resumen en un documento mínimo autónomo."""
    safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!DOCTYPE html>\n<html lang=\"es\">\n<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n</head>\n<body>\n"
        f"<h1>{safe_title}</h1>\n{body_html}\n</body>\n</html>\n"
    )
