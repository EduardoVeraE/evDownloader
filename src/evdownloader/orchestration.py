"""Typed course traversal and unit coordination; no provider or HTTP dispatch."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from rich.console import Console

from .config import Settings
from .models import Course, Unit, UnitType
from .resource_download import exception_name
from .results import DownloadOutcome, FailedArtifact, UnitOutcome, VideoOutcome
from .utils import numbered, safe_mkdir, slugify

UnitProcessor = Callable[[Unit, Path, Settings], Awaitable[UnitOutcome]]
FailureRecorder = Callable[[list[FailedArtifact] | None, str, str], None]


class VideoOperation(Protocol):
    def __call__(
        self,
        unit: Unit,
        base: Path,
        settings: Settings,
        *,
        failed_artifacts: list[FailedArtifact] | None = None,
    ) -> Awaitable[VideoOutcome]: ...


class ExtrasOperation(Protocol):
    def __call__(
        self,
        unit: Unit,
        base: Path,
        settings: Settings,
        *,
        failed_artifacts: list[FailedArtifact] | None = None,
    ) -> Awaitable[int]: ...


async def run_course(
    course: Course,
    platform: str,
    settings: Settings,
    process: UnitProcessor,
    console: Console,
) -> DownloadOutcome:
    console.print(
        f"[bold cyan]{course.title}[/bold cyan] — "
        f"{sum(len(c.units) for c in course.chapters)} unidades en "
        f"{len(course.chapters)} capítulos"
    )
    course_dir = safe_mkdir(settings.download_dir / platform.capitalize() / slugify(course.title))
    downloaded = 0
    outcome = DownloadOutcome()
    for chapter in course.chapters:
        chapter_dir = safe_mkdir(course_dir / numbered(chapter.index, chapter.title))
        for unit in chapter.units:
            if (
                unit.type is UnitType.VIDEO
                and settings.limit is not None
                and downloaded >= settings.limit
            ):
                console.print(f"[dim]Límite de {settings.limit} clases alcanzado.[/dim]")
                return outcome
            outcome.add(await process(unit, chapter_dir, settings), chapter_index=chapter.index)
            if unit.type is UnitType.VIDEO:
                downloaded += 1
                if settings.limit is not None and downloaded >= settings.limit:
                    console.print(f"[dim]Límite de {settings.limit} clases alcanzado.[/dim]")
                    return outcome
    return outcome


async def process_unit(
    unit: Unit,
    out_dir: Path,
    settings: Settings,
    *,
    video: VideoOperation,
    extras: ExtrasOperation,
    record_failure: FailureRecorder,
    console: Console,
) -> UnitOutcome:
    base = out_dir / numbered(unit.index, unit.title)
    failures: list[FailedArtifact] = []
    video_failures = 0
    subtitle_failures = 0
    if unit.type == UnitType.VIDEO:
        video_ok, subtitles_ok = await video(unit, base, settings, failed_artifacts=failures)
        video_failures = int(not video_ok)
        subtitle_failures = int(video_ok and not subtitles_ok)
    else:
        console.print(f"[dim]{unit.type.value}: {unit.title}[/dim]")
    extras_failures = 0
    resource_failures = 0
    if settings.resources:
        prior_failures = len(failures)
        try:
            resource_failures = await extras(unit, base, settings, failed_artifacts=failures)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗ Extras failed ({exception_name(exc)}).[/red]")
            extras_failures = 1
            if len(failures) == prior_failures:
                record_failure(
                    failures,
                    f"Unidad {unit.index} — {unit.title} / extras (captura de recursos)",
                    exception_name(exc),
                )
    return UnitOutcome(
        video_failures, extras_failures, resource_failures, subtitle_failures, tuple(failures)
    )
