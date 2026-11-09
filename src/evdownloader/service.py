"""Orquestación de la descarga de un curso completo.

Une extractor (estructura + resolución de video por interceptación) con el
motor de descarga elegido, y organiza la salida en carpetas jerárquicas.
"""

from __future__ import annotations

import json
import os as os  # Compatibility for callers patching the publication failure seam.
import re
import stat
from functools import partial
from pathlib import Path
from urllib.parse import unquote, urlsplit

import rnet as rnet  # Preserve the legacy injected HTTP client seam.
from playwright.async_api import BrowserContext
from rich.console import Console

from . import browser, cache, orchestration
from .artifacts import artifact_path as _artifact_path
from .artifacts import publish_atomic_text
from .artifacts import write_text as _write_text
from .config import Settings
from .downloaders import get_downloader
from .downloaders.base import Downloader
from .extractors import get_extractor
from .extractors.base import Extractor
from .extractors.udemy import UdemyExtractor
from .models import (
    Cookie,
    Course,
    Resource,
    ResourceKind,
    Subtitle,
    Unit,
    UnitExtras,
    UnitType,
    VideoSource,
)
from .resource_download import download_resource
from .resource_download import exception_name as _exception_name
from .results import (
    DownloadOutcome,
    FailedArtifact,
    SubtitleFailure,
    SubtitleSaveReport,
    UnitOutcome,
    VideoOutcome,
)
from .results import record_failure as _record_failure
from .subtitle_transfer import MAX_SUBTITLE_BYTES as _MAX_SUBTITLE_BYTES
from .subtitle_transfer import fetch_subtitle as _fetch_subtitle
from .subtitle_transfer import subtitle_client as _subtitle_client
from .utils import safe_mkdir, slugify

console = Console()


class DrmPreflightError(ValueError):
    """Invalid DRM device configuration detected before course processing."""


# Extensión "limpia" al final de un nombre (p. ej. ".pdf", ".rar"): distingue un
# título que ya es un nombre de archivo de uno descriptivo con puntos.
_CLEAN_EXT_RE = re.compile(r"^\.[a-zA-Z0-9]{1,5}$")
_WEBVTT_LEADING_WHITESPACE = b" \t\r\n"


def _print_failed_artifacts(outcome: DownloadOutcome) -> None:
    if outcome.failed_artifacts:
        console.print("[bold yellow]Archivos y recursos no descargados:[/bold yellow]")
        for failure in outcome.failed_artifacts:
            console.print(f"  - {failure.artifact}: {failure.reason}", markup=False)


async def download_course(url: str, settings: Settings, *, use_cache: bool = False) -> None:
    """Descarga un curso completo a ``settings.download_dir``."""
    extractor = get_extractor(url)
    if settings.use_drm:
        if settings.drm_device is not None:
            try:
                valid_device = settings.drm_device.is_file()
            except OSError:
                valid_device = False
            if not valid_device:
                raise DrmPreflightError(
                    "Invalid --drm-device: provide a path to an existing regular .wvd file."
                )
        elif isinstance(extractor, UdemyExtractor):
            raise DrmPreflightError(
                "Udemy --use-drm requires --drm-device with a regular .wvd file."
            )
    extractor.configure(settings)
    downloader = get_downloader(settings.downloader)

    if extractor.needs_browser:
        async with browser.browser_context(
            headless=settings.headless, platform=extractor.name
        ) as ctx:
            outcome = await _run_download(
                extractor, downloader, ctx, url, settings, use_cache=use_cache
            )
    else:
        # Extractores que delegan en yt-dlp (Udemy) no abren navegador.
        outcome = await _run_download(
            extractor, downloader, None, url, settings, use_cache=use_cache
        )

    if outcome.incomplete:
        summary = outcome.summary
        console.print(f"[bold red]Download incomplete: {summary}.[/bold red]")
        _print_failed_artifacts(outcome)
        raise RuntimeError(f"Download incomplete: {summary}.")
    console.print("[bold green]Descarga finalizada.[/bold green]")
    _print_failed_artifacts(outcome)


async def _run_download(
    extractor: Extractor,
    downloader: Downloader,
    ctx: BrowserContext | None,
    url: str,
    settings: Settings,
    *,
    use_cache: bool,
) -> DownloadOutcome:
    course = await _load_structure(extractor, ctx, url, use_cache=use_cache)
    return await orchestration.run_course(
        course,
        extractor.name,
        settings,
        partial(_process_unit, extractor, downloader, ctx),
        console,
    )


async def _load_structure(
    extractor: Extractor, ctx: BrowserContext | None, url: str, *, use_cache: bool
) -> Course:
    cache_url = _structure_cache_key(extractor, url)
    if use_cache:
        cached = cache.get(cache_url)
        if cached:
            cached_course = Course.model_validate(cached)
            if any(chapter.units for chapter in cached_course.chapters):
                console.print("[dim]Estructura cargada desde caché.[/dim]")
                return cached_course
    course = await extractor.list_course(ctx, url)
    if not any(chapter.units for chapter in course.chapters):
        raise ValueError(
            "No se encontraron unidades descargables. Verifica el acceso al curso "
            "e inténtalo de nuevo."
        )
    cache.set(cache_url, course.model_dump())
    return course


def _structure_cache_key(extractor: Extractor, url: str) -> str:
    revision = getattr(type(extractor), "structure_cache_revision", None)
    if not isinstance(revision, str) or not revision:
        return url
    return f"{url}#evd-structure={revision}"


async def _process_unit(
    extractor: Extractor,
    downloader: Downloader,
    ctx: BrowserContext | None,
    unit: Unit,
    out_dir: Path,
    settings: Settings,
) -> UnitOutcome:
    return await orchestration.process_unit(
        unit,
        out_dir,
        settings,
        video=partial(_download_video, extractor, downloader, ctx),
        extras=partial(_save_extras, extractor, ctx),
        record_failure=_record_failure,
        console=console,
    )


async def _download_video(
    extractor: Extractor,
    downloader: Downloader,
    ctx: BrowserContext | None,
    unit: Unit,
    base: Path,
    settings: Settings,
    *,
    failed_artifacts: list[FailedArtifact] | None = None,
) -> VideoOutcome:
    label = f"Unidad {unit.index} — {unit.title}"
    canonical_video = _artifact_path(base, ".mp4")
    legacy_video = base.with_suffix(".mp4")
    existing_video = canonical_video if canonical_video.exists() else None
    if existing_video is None and legacy_video != canonical_video and legacy_video.exists():
        existing_video = legacy_video
    recovering = existing_video is not None and not settings.overwrite
    prior_manifest = _read_subtitle_manifest(base) if recovering else None
    recheck_empty_udemy_manifest = (
        settings.use_drm
        and isinstance(extractor, UdemyExtractor)
        and prior_manifest is not None
        and not prior_manifest[1]
    )
    if (
        recovering
        and not recheck_empty_udemy_manifest
        and _subtitle_manifest_satisfied(base, prior_manifest)
    ):
        assert existing_video is not None
        console.print(f"[dim]Ya existe: {existing_video.name}[/dim]")
        return VideoOutcome(True, True)
    prior_paths = prior_manifest[1] if prior_manifest is not None else ()

    try:
        _write_subtitle_manifest(base, complete=False, paths=prior_paths)
    except OSError as exc:
        console.print(
            f"[red]✗ No se pudo preparar subtítulos para {unit.title} "
            f"({_exception_name(exc)}).[/red]"
        )
        _record_failure(
            failed_artifacts, f"{label} / preparación de subtítulos", _exception_name(exc)
        )
        return VideoOutcome(recovering, False)

    action = "Recuperando subtítulos" if recovering else "Resolviendo"
    console.print(f"[cyan]{action}:[/cyan] {unit.title}")
    try:
        source = await extractor.resolve_video(ctx, unit)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ No se pudo resolver {unit.title} ({_exception_name(exc)}).[/red]")
        _record_failure(failed_artifacts, f"{label} / resolución de fuente", _exception_name(exc))
        return VideoOutcome(recovering, False)
    if source is None:
        console.print(f"[red]Sin fuente de video para:[/red] {unit.title}")
        _record_failure(failed_artifacts, f"{label} / fuente", "missing_video_source")
        return VideoOutcome(recovering, False)

    if recovering:
        complete, paths, failures = await _collect_source_subtitles(
            downloader,
            source,
            base,
            settings,
            recover_managed=True,
            failed_artifacts=failed_artifacts,
            label=label,
        )
    else:
        try:
            path = await downloader.download(source, base, settings)
            console.print(f"[green]✓[/green] {path.name}")
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[red]✗ Falló la descarga de {unit.title} ({_exception_name(exc)}).[/red]"
            )
            _record_failure(failed_artifacts, f"{label} / video", _exception_name(exc))
            return VideoOutcome(False, False)
        complete, paths, failures = await _collect_source_subtitles(
            downloader,
            source,
            base,
            settings,
            recover_managed=False,
            failed_artifacts=failed_artifacts,
            label=label,
        )

    if recovering:
        paths = _stable_path_union(prior_paths, paths)
    if any(not _valid_webvtt(path) for path in paths):
        complete = False
        for path in paths:
            if not _valid_webvtt(path) and path not in _subtitle_output_paths(
                source.subtitles, base
            ):
                _record_failure(
                    failed_artifacts, f"{label} / {path.name}", "missing_or_invalid_webvtt"
                )
        if "invalid_managed_subtitle" not in failures:
            failures = (*failures, "missing_or_invalid_webvtt")

    try:
        _write_subtitle_manifest(base, complete=complete, paths=paths)
    except OSError as exc:
        failures = (*failures, _exception_name(exc))
        _record_failure(
            failed_artifacts, f"{label} / manifiesto de subtítulos", _exception_name(exc)
        )
        complete = False

    if complete:
        label = "recuperados" if recovering else "guardados"
        console.print(f"[green]✓ Subtítulos {label}:[/green] {len(paths)}")
    else:
        detail = ", ".join(failures) if failures else "estado incompleto"
        console.print(f"[yellow]Subtítulos incompletos:[/yellow] {detail}")
    return VideoOutcome(True, complete)


async def _collect_source_subtitles(
    downloader: Downloader,
    source: VideoSource,
    base: Path,
    settings: Settings,
    *,
    recover_managed: bool,
    failed_artifacts: list[FailedArtifact] | None = None,
    label: str = "Subtítulos",
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

    for reason in failures:
        _record_failure(failed_artifacts, f"{label} / subtítulos gestionados", reason)

    if source.subtitles:
        expected_paths = _subtitle_output_paths(source.subtitles, base)
        try:
            report = await _save_subtitles(source.subtitles, base, source)
        except Exception as exc:  # noqa: BLE001
            complete = False
            failures.append(_exception_name(exc))
            for index, _ in enumerate(source.subtitles, 1):
                _record_failure(
                    failed_artifacts, f"{label} / subtítulo {index}", _exception_name(exc)
                )
        else:
            if report.failures:
                complete = False
                failures.extend(_subtitle_failure_label(failure) for failure in report.failures)
                for failure in report.failures:
                    reason = failure.reason
                    if failure.http_status is not None:
                        reason += f" (HTTP {failure.http_status})"
                    _record_failure(
                        failed_artifacts,
                        f"{label} / subtítulo {failure.track_index + 1} ({failure.lang})",
                        reason,
                    )
        paths = tuple(dict.fromkeys((*paths, *expected_paths)))
    elif not source.write_subs and not source.subtitle_inventory_confirmed:
        complete = False
        _record_failure(
            failed_artifacts, f"{label} / inventario de subtítulos", "subtitle_inventory_unknown"
        )

    if source.subtitle_inventory_required and not source.subtitle_inventory_confirmed:
        complete = False
        failures.append("subtitle_inventory_unknown")
        if source.write_subs or source.subtitles:
            _record_failure(
                failed_artifacts,
                f"{label} / inventario de subtítulos",
                "subtitle_inventory_unknown",
            )

    return complete, paths, tuple(failures)


def _subtitle_failure_label(failure: SubtitleFailure) -> str:
    if failure.http_status is not None:
        return f"{failure.reason} (HTTP {failure.http_status})"
    return failure.reason


def _subtitle_manifest_path(base: Path) -> Path:
    return _artifact_path(base, ".subtitles.json")


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
    publish_atomic_text(manifest_path, json.dumps(payload, separators=(",", ":")) + "\n")


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


async def _save_extras(
    extractor: Extractor,
    ctx: BrowserContext | None,
    unit: Unit,
    base: Path,
    settings: Settings,
    *,
    failed_artifacts: list[FailedArtifact] | None = None,
) -> int:
    """Guarda resumen, archivos adjuntos, enlaces y, si aplica, el MHTML."""
    # Para lectures/quizzes (sin video) capturamos la página completa como MHTML.
    capture = unit.type is not UnitType.VIDEO
    extras: UnitExtras = await extractor.resolve_extras(ctx, unit, capture_page=capture)
    if extras.page_capture_failed:
        _record_failure(
            failed_artifacts,
            f"Unidad {unit.index} — {unit.title} / .mhtml",
            "page_capture_failed_or_unavailable",
        )

    async def write_extra(suffix: str, content: str) -> None:
        try:
            await _write_text(_artifact_path(base, suffix), content)
        except Exception as exc:  # noqa: BLE001
            _record_failure(
                failed_artifacts,
                f"Unidad {unit.index} — {unit.title} / {suffix}",
                _exception_name(exc),
            )
            raise

    if extras.summary_html:
        await write_extra(".resumen.html", _wrap_html(unit.title, extras.summary_html))

    if extras.page_mhtml:
        await write_extra(".mhtml", extras.page_mhtml)

    files = [r for r in extras.resources if r.kind is ResourceKind.FILE]
    links = [r for r in extras.resources if r.kind is ResourceKind.LINK]

    if links:
        body = "\n".join(f"- [{lk.title}]({lk.url})" for lk in links)
        await write_extra(".enlaces.md", f"# Enlaces — {unit.title}\n\n{body}\n")

    if files:
        try:
            cookies = browser.cookies_as_records(await ctx.cookies()) if ctx is not None else []
            res_dir = safe_mkdir(_artifact_path(base, "-recursos"))
        except Exception as exc:  # noqa: BLE001
            for index, resource in enumerate(files, 1):
                _record_failure(
                    failed_artifacts,
                    f"Unidad {unit.index} — {unit.title} / adjunto {index} — {resource.title}",
                    _exception_name(exc),
                )
            raise
        if failed_artifacts is None:
            return await _download_files(files, res_dir, cookies, extractor.resource_host_suffixes)
        return await _download_files(
            files,
            res_dir,
            cookies,
            extractor.resource_host_suffixes,
            failed_artifacts=failed_artifacts,
            label=f"Unidad {unit.index} — {unit.title}",
        )
    return 0


async def _save_subtitles(
    subtitles: list[Subtitle], base: Path, source: VideoSource
) -> SubtitleSaveReport:
    expected_paths = _subtitle_output_paths(subtitles, base)
    if not subtitles:
        return SubtitleSaveReport(attempted=0, expected_paths=(), saved_paths=(), failures=())
    client = _subtitle_client()
    saved_paths: list[Path] = []
    failures: list[SubtitleFailure] = []
    data: bytes | None
    status_code: int | None
    for track_index, (sub, sub_path) in enumerate(zip(subtitles, expected_paths, strict=True)):
        if sub.content is not None:
            data, status_code, declared_header, request_failure = (
                sub.content.encode("utf-8"),
                200,
                None,
                None,
            )
        else:
            data, status_code, declared_header, request_failure = await _fetch_subtitle(
                client, sub.url, source
            )
        if request_failure:
            failure_size = None
            if request_failure == "declared_too_large" and declared_header is not None:
                failure_size = int(declared_header)
            failures.append(
                SubtitleFailure(
                    track_index,
                    sub.lang,
                    request_failure,
                    http_status=status_code,
                    size=failure_size,
                )
            )
            continue
        assert data is not None and status_code is not None

        declared_size: int | None = None
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

        try:
            publish_atomic_text(sub_path, content)
        except OSError:
            failures.append(
                SubtitleFailure(
                    track_index, sub.lang, "write_error", http_status=status_code, size=size
                )
            )
            continue
        saved_paths.append(sub_path)

    return SubtitleSaveReport(
        attempted=len(subtitles),
        expected_paths=expected_paths,
        saved_paths=tuple(saved_paths),
        failures=tuple(failures),
    )


async def _download_files(
    files: list[Resource],
    out_dir: Path,
    cookies: list[Cookie],
    trusted_host_suffixes: tuple[str, ...],
    *,
    failed_artifacts: list[FailedArtifact] | None = None,
    label: str = "Recursos",
) -> int:
    """Descarga adjuntos sin revelar URLs firmadas ni credenciales en errores."""
    failures = 0
    for index, res in enumerate(files, 1):
        artifact = f"{label} / adjunto {index} — {res.title}"
        name = _filename_from_url(res.url, res.title)
        try:
            result = await download_resource(
                res.url,
                out_dir / name,
                cookie_jar=cookies,
                trusted_host_suffixes=trusted_host_suffixes,
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _record_failure(failed_artifacts, artifact, _exception_name(exc))
            console.print(f"  [red]·[/red] resource failed ({_exception_name(exc)})")
            continue
        if result.ok:
            console.print(f"  [green]·[/green] recurso: {name}")
        else:
            failures += 1
            error = f" ({result.error_name})" if result.error_name else ""
            reason = result.reason or "resource_failed"
            if isinstance(result.http_status, int):
                reason += f" (HTTP {result.http_status})"
            _record_failure(failed_artifacts, artifact, reason + error)
            console.print(f"  [red]·[/red] falló recurso: {result.reason}{error}")
    return failures


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


def _wrap_html(title: str, body_html: str) -> str:
    """Envuelve el HTML del resumen en un documento mínimo autónomo."""
    safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n</head>\n<body>\n"
        f"<h1>{safe_title}</h1>\n{body_html}\n</body>\n</html>\n"
    )
