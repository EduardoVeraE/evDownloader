"""Orquestación de la descarga de un curso completo.

Une extractor (estructura + resolución de video por interceptación) con el
motor de descarga elegido, y organiza la salida en carpetas jerárquicas.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiofiles
import rnet
from rich.console import Console

from . import browser, cache
from .config import RNET_IMPERSONATE, Settings
from .downloaders import get_downloader
from .extractors import get_extractor
from .models import (
    Course,
    Resource,
    ResourceKind,
    Subtitle,
    Unit,
    UnitExtras,
    UnitType,
    VideoSource,
)
from .utils import numbered, safe_mkdir, slugify

console = Console()

# Extensión "limpia" al final de un nombre (p. ej. ".pdf", ".rar"): distingue un
# título que ya es un nombre de archivo de uno descriptivo con puntos.
_CLEAN_EXT_RE = re.compile(r"^\.[a-zA-Z0-9]{1,5}$")
_MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
_WEBVTT_LEADING_WHITESPACE = b" \t\r\n"
_REQUEST_TIMEOUT_S = 30
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 30


@dataclass(frozen=True, slots=True)
class SubtitleFailure:
    track_index: int
    lang: str
    reason: str
    http_status: int | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class SubtitleSaveReport:
    attempted: int
    expected_paths: tuple[Path, ...]
    saved_paths: tuple[Path, ...]
    failures: tuple[SubtitleFailure, ...]

    @property
    def saved_count(self) -> int:
        return len(self.saved_paths)


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
    canonical_video = _artifact_path(base, ".mp4")
    legacy_video = base.with_suffix(".mp4")
    existing_video = canonical_video if canonical_video.exists() else None
    if existing_video is None and legacy_video != canonical_video and legacy_video.exists():
        existing_video = legacy_video
    recovering = existing_video is not None and not settings.overwrite
    prior_manifest = _read_subtitle_manifest(base) if recovering else None
    if recovering and _subtitle_manifest_satisfied(base, prior_manifest):
        assert existing_video is not None
        console.print(f"[dim]Ya existe: {existing_video.name}[/dim]")
        return
    prior_paths = prior_manifest[1] if prior_manifest is not None else ()

    try:
        _write_subtitle_manifest(base, complete=False, paths=prior_paths)
    except OSError as exc:
        console.print(
            f"[red]✗ No se pudo preparar subtítulos para {unit.title} "
            f"({_exception_name(exc)}).[/red]"
        )
        return

    action = "Recuperando subtítulos" if recovering else "Resolviendo"
    console.print(f"[cyan]{action}:[/cyan] {unit.title}")
    try:
        source = await extractor.resolve_video(ctx, unit)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ No se pudo resolver {unit.title} ({_exception_name(exc)}).[/red]")
        return
    if source is None:
        console.print(f"[red]Sin fuente de video para:[/red] {unit.title}")
        return

    if recovering:
        complete, paths, failures = await _collect_source_subtitles(
            downloader, source, base, settings, recover_managed=True
        )
    else:
        try:
            path = await downloader.download(source, base, settings)
            console.print(f"[green]✓[/green] {path.name}")
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[red]✗ Falló la descarga de {unit.title} ({_exception_name(exc)}).[/red]"
            )
            return
        complete, paths, failures = await _collect_source_subtitles(
            downloader, source, base, settings, recover_managed=False
        )

    if recovering:
        paths = _stable_path_union(prior_paths, paths)
    if any(not _valid_webvtt(path) for path in paths):
        complete = False
        if "invalid_managed_subtitle" not in failures:
            failures = (*failures, "missing_or_invalid_webvtt")

    try:
        _write_subtitle_manifest(base, complete=complete, paths=paths)
    except OSError as exc:
        failures = (*failures, _exception_name(exc))
        complete = False

    if complete:
        label = "recuperados" if recovering else "guardados"
        console.print(f"[green]✓ Subtítulos {label}:[/green] {len(paths)}")
    else:
        detail = ", ".join(failures) if failures else "estado incompleto"
        console.print(f"[yellow]Subtítulos incompletos:[/yellow] {detail}")


async def _collect_source_subtitles(
    downloader,
    source: VideoSource,
    base: Path,
    settings: Settings,
    *,
    recover_managed: bool,
) -> tuple[bool, tuple[Path, ...], tuple[str, ...]]:
    complete = True
    paths: tuple[Path, ...] = ()
    failures: list[str] = []

    if source.write_subs:
        if getattr(downloader, "supports_managed_subtitles", False) is not True:
            complete = False
            failures.append("managed_subtitles_unsupported")
        elif recover_managed:
            try:
                recovered = await downloader.download_subtitles(source, base, settings)
            except Exception as exc:  # noqa: BLE001
                complete = False
                failures.append(_exception_name(exc))
            else:
                paths, valid_paths = _managed_subtitle_paths(base, recovered)
                if not valid_paths:
                    complete = False
                    failures.append("invalid_managed_subtitle_path")
        else:
            try:
                paths, valid_paths = _discover_subtitle_paths(base)
            except (OSError, ValueError) as exc:
                complete = False
                failures.append(_exception_name(exc))
            else:
                if not valid_paths:
                    complete = False
                    failures.append("invalid_managed_subtitle")

    if source.subtitles:
        expected_paths = _subtitle_output_paths(source.subtitles, base)
        try:
            report = await _save_subtitles(source.subtitles, base, source)
        except Exception as exc:  # noqa: BLE001
            complete = False
            failures.append(_exception_name(exc))
        else:
            if report.failures:
                complete = False
                failures.extend(_subtitle_failure_label(failure) for failure in report.failures)
        paths = tuple(dict.fromkeys((*paths, *expected_paths)))
    elif not source.write_subs:
        complete = False

    return complete, paths, tuple(failures)


def _subtitle_failure_label(failure: SubtitleFailure) -> str:
    if failure.http_status is not None:
        return f"{failure.reason} (HTTP {failure.http_status})"
    return failure.reason


def _exception_name(exc: BaseException) -> str:
    return type(exc).__name__


def _subtitle_manifest_path(base: Path) -> Path:
    return _artifact_path(base, ".subtitles.json")


def _artifact_path(base: Path, suffix: str) -> Path:
    return base.parent / f"{base.name}{suffix}"


def _is_subtitle_basename(base: Path, name: str) -> bool:
    prefix = f"{base.name}."
    return (
        bool(name)
        and not any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in name)
        and not Path(name).is_absolute()
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
        and name.startswith(prefix)
        and name.endswith(".vtt")
        and len(name) > len(prefix) + len(".vtt")
    )


def _read_subtitle_manifest(base: Path) -> tuple[bool, tuple[Path, ...]] | None:
    try:
        payload = json.loads(_subtitle_manifest_path(base).read_text(encoding="utf-8"))
    except OSError, ValueError, UnicodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"version", "complete", "files"}:
        return None
    if type(payload["version"]) is not int or payload["version"] != 1:
        return None
    if type(payload["complete"]) is not bool or not isinstance(payload["files"], list):
        return None

    names = payload["files"]
    if any(not isinstance(name, str) or not _is_subtitle_basename(base, name) for name in names):
        return None
    if len(set(names)) != len(names):
        return None
    return payload["complete"], tuple(base.parent / name for name in names)


def _subtitle_manifest_satisfied(
    base: Path, manifest: tuple[bool, tuple[Path, ...]] | None = None
) -> bool:
    manifest = _read_subtitle_manifest(base) if manifest is None else manifest
    return manifest is not None and manifest[0] and all(_valid_webvtt(path) for path in manifest[1])


def _write_subtitle_manifest(base: Path, *, complete: bool, paths: tuple[Path, ...]) -> None:
    manifest_path = _subtitle_manifest_path(base)
    names = [path.name for path in paths if _is_subtitle_basename(base, path.name)]
    payload = {"version": 1, "complete": complete, "files": names}
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, separators=(",", ":"))
            temporary.write("\n")
        os.replace(temporary_name, manifest_path)
    finally:
        if temporary_name is not None:
            with contextlib.suppress(OSError):
                Path(temporary_name).unlink()


def _discover_subtitle_paths(base: Path) -> tuple[tuple[Path, ...], bool]:
    paths = tuple(path for path in base.parent.iterdir() if _is_subtitle_basename(base, path.name))
    paths = tuple(sorted(paths, key=lambda path: path.name))
    return paths, all(_valid_webvtt(path) for path in paths)


def _managed_subtitle_paths(base: Path, paths: list[Path]) -> tuple[tuple[Path, ...], bool]:
    accepted: list[Path] = []
    valid = True
    for path in paths:
        if (
            not isinstance(path, Path)
            or path.parent != base.parent
            or not _is_subtitle_basename(base, path.name)
        ):
            valid = False
            continue
        if path not in accepted:
            accepted.append(path)
    return tuple(accepted), valid


def _stable_path_union(*groups: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(path for group in groups for path in group))


def _decode_webvtt(data: bytes) -> str | None:
    if len(data) > _MAX_SUBTITLE_BYTES:
        return None
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    content = content.lstrip(_WEBVTT_LEADING_WHITESPACE.decode())
    if not content.startswith("WEBVTT") or (len(content) > 6 and content[6] not in " \t\r\n"):
        return None
    return content


def _valid_webvtt(path: Path) -> bool:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SUBTITLE_BYTES:
            return False
        with path.open("rb") as subtitle_file:
            data = subtitle_file.read(_MAX_SUBTITLE_BYTES + 1)
    except OSError, ValueError:
        return False
    return _decode_webvtt(data) is not None


def _subtitle_output_paths(subtitles: list[Subtitle], base: Path) -> tuple[Path, ...]:
    language_counts: dict[str, int] = {}
    paths: list[Path] = []
    for subtitle in subtitles:
        language = slugify(subtitle.lang, max_len=40)
        occurrence = language_counts.get(language, 0) + 1
        language_counts[language] = occurrence
        suffix = "" if occurrence == 1 else f"-{occurrence}"
        paths.append(_artifact_path(base, f".{language}{suffix}.vtt"))
    return tuple(paths)


async def _save_extras(extractor, ctx, unit: Unit, base: Path, settings: Settings) -> None:
    """Guarda resumen, archivos adjuntos, enlaces y, si aplica, el MHTML."""
    # Para lectures/quizzes (sin video) capturamos la página completa como MHTML.
    capture = unit.type is not UnitType.VIDEO
    extras: UnitExtras = await extractor.resolve_extras(ctx, unit, capture_page=capture)

    if extras.summary_html:
        await _write_text(
            _artifact_path(base, ".resumen.html"), _wrap_html(unit.title, extras.summary_html)
        )

    if extras.page_mhtml:
        await _write_text(_artifact_path(base, ".mhtml"), extras.page_mhtml)

    files = [r for r in extras.resources if r.kind is ResourceKind.FILE]
    links = [r for r in extras.resources if r.kind is ResourceKind.LINK]

    if links:
        body = "\n".join(f"- [{lk.title}]({lk.url})" for lk in links)
        await _write_text(
            _artifact_path(base, ".enlaces.md"), f"# Enlaces — {unit.title}\n\n{body}\n"
        )

    if files:
        # Udemy no abre navegador (ctx=None); sus URLs de recurso vienen firmadas.
        cookies = browser.cookies_as_dict(await ctx.cookies()) if ctx is not None else {}
        res_dir = safe_mkdir(_artifact_path(base, "-recursos"))
        await _download_files(files, res_dir, cookies)


async def _save_subtitles(
    subtitles: list[Subtitle], base: Path, source: VideoSource
) -> SubtitleSaveReport:
    expected_paths = _subtitle_output_paths(subtitles, base)
    if not subtitles:
        return SubtitleSaveReport(attempted=0, expected_paths=(), saved_paths=(), failures=())
    client = _subtitle_client()
    saved_paths: list[Path] = []
    failures: list[SubtitleFailure] = []
    for track_index, (sub, sub_path) in enumerate(zip(subtitles, expected_paths, strict=True)):
        headers = {
            key: value for key, value in source.http_headers.items() if key.lower() != "cookie"
        }
        cookie_header = browser.cookie_header_for_url(source.cookie_jar, sub.url)
        if cookie_header:
            headers["Cookie"] = cookie_header

        try:
            resp = await client.get(sub.url, headers=headers)
        except Exception:  # noqa: BLE001
            failures.append(SubtitleFailure(track_index, sub.lang, "network_error"))
            continue

        status = resp.status_code
        status_code = status.as_int()
        if not status.is_success():
            failures.append(
                SubtitleFailure(track_index, sub.lang, "http_status", http_status=status_code)
            )
            continue

        declared_size: int | None = None
        declared_header = resp.headers.get("content-length")
        if declared_header is not None:
            try:
                declared_size = int(declared_header)
            except TypeError, ValueError:
                declared_size = None
        if declared_size is not None and declared_size > _MAX_SUBTITLE_BYTES:
            failures.append(
                SubtitleFailure(
                    track_index,
                    sub.lang,
                    "declared_too_large",
                    http_status=status_code,
                    size=declared_size,
                )
            )
            continue

        try:
            data = await resp.bytes()
        except Exception:  # noqa: BLE001
            failures.append(
                SubtitleFailure(track_index, sub.lang, "read_error", http_status=status_code)
            )
            continue

        size = len(data)
        content = _decode_webvtt(data)
        if size > _MAX_SUBTITLE_BYTES:
            failures.append(
                SubtitleFailure(
                    track_index,
                    sub.lang,
                    "body_too_large",
                    http_status=status_code,
                    size=size,
                )
            )
            continue
        if not data:
            failures.append(
                SubtitleFailure(track_index, sub.lang, "empty", http_status=status_code, size=size)
            )
            continue
        if content is None:
            reason = "invalid_utf8"
            try:
                data.decode("utf-8-sig")
            except UnicodeDecodeError:
                pass
            else:
                reason = "invalid_vtt"
            failures.append(
                SubtitleFailure(track_index, sub.lang, reason, http_status=status_code, size=size)
            )
            continue

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=sub_path.parent,
                prefix=f".{sub_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(content)
            os.replace(temporary_name, sub_path)
        except OSError:
            failures.append(
                SubtitleFailure(
                    track_index, sub.lang, "write_error", http_status=status_code, size=size
                )
            )
            continue
        finally:
            if temporary_name is not None:
                with contextlib.suppress(OSError):
                    Path(temporary_name).unlink()
        saved_paths.append(sub_path)

    return SubtitleSaveReport(
        attempted=len(subtitles),
        expected_paths=expected_paths,
        saved_paths=tuple(saved_paths),
        failures=tuple(failures),
    )


async def _download_files(files: list[Resource], out_dir: Path, cookies: dict[str, str]) -> None:
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


def _subtitle_client():
    return rnet.Client(
        impersonate=getattr(rnet.Impersonate, RNET_IMPERSONATE, None),
        timeout=_REQUEST_TIMEOUT_S,
        connect_timeout=_CONNECT_TIMEOUT_S,
        read_timeout=_READ_TIMEOUT_S,
    )


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
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n</head>\n<body>\n"
        f"<h1>{safe_title}</h1>\n{body_html}\n</body>\n</html>\n"
    )
