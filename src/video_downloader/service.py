"""Orquestación de la descarga de un curso completo.

Une extractor (estructura + resolución de video por interceptación) con el
motor de descarga elegido, y organiza la salida en carpetas jerárquicas.
"""

from __future__ import annotations

from pathlib import Path

import aiofiles
import rnet
from rich.console import Console

from . import browser, cache
from .config import RNET_IMPERSONATE, Settings
from .downloaders import get_downloader
from .extractors import get_extractor
from .models import Course, Subtitle, Unit, UnitType
from .utils import numbered, safe_mkdir, slugify

console = Console()


async def download_course(url: str, settings: Settings, *, use_cache: bool = True) -> None:
    """Descarga un curso completo a ``settings.download_dir``."""
    extractor = get_extractor(url)
    downloader = get_downloader(settings.downloader)

    async with browser.browser_context(headless=settings.headless) as ctx:
        course = await _load_structure(extractor, ctx, url, use_cache=use_cache)
        console.print(
            f"[bold cyan]{course.title}[/bold cyan] — "
            f"{sum(len(c.units) for c in course.chapters)} unidades en "
            f"{len(course.chapters)} capítulos"
        )

        course_dir = safe_mkdir(settings.download_dir / slugify(course.title))

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

    console.print("[bold green]Descarga finalizada.[/bold green]")


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
    if unit.type != UnitType.VIDEO:
        console.print(f"[dim]Omitiendo {unit.type.value}: {unit.title}[/dim]")
        return

    base = out_dir / numbered(unit.index, unit.title)
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
