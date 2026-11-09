"""Download result contracts and aggregation, independent of providers and I/O."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


class VideoOutcome(NamedTuple):
    """Required media and subtitle completeness are independent observations."""

    video_ok: bool
    subtitles_ok: bool


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


@dataclass(frozen=True, slots=True)
class FailedArtifact:
    artifact: str
    reason: str


def record_failure(failures: list[FailedArtifact] | None, artifact: str, reason: str) -> None:
    if failures is not None:
        # Never derive identifiers from signed URLs or render exception messages.
        artifact = re.sub(r"(?i)https?://\S+", "<URL omitida>", artifact)
        artifact = "".join(char if char.isprintable() else " " for char in artifact)
        failures.append(FailedArtifact(artifact, reason))


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    video_failures: int = 0
    extras_failures: int = 0
    resource_failures: int = 0
    subtitle_failures: int = 0
    failed_artifacts: tuple[FailedArtifact, ...] = ()


@dataclass(slots=True)
class DownloadOutcome:
    video_failures: int = 0
    extras_failures: int = 0
    resource_failures: int = 0
    subtitle_failures: int = 0
    failed_artifacts: list[FailedArtifact] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        """Keep the current strict exit policy, including required sidecars."""
        return any(
            (
                self.video_failures,
                self.extras_failures,
                self.resource_failures,
                self.subtitle_failures,
            )
        )

    @property
    def summary(self) -> str:
        return (
            f"videos={self.video_failures}, extras={self.extras_failures}, "
            f"resources={self.resource_failures}, subtitles={self.subtitle_failures}"
        )

    def add(self, unit: UnitOutcome, *, chapter_index: int | None = None) -> None:
        self.video_failures += unit.video_failures
        self.extras_failures += unit.extras_failures
        self.resource_failures += unit.resource_failures
        self.subtitle_failures += unit.subtitle_failures
        self.failed_artifacts.extend(
            FailedArtifact(
                f"Capítulo {chapter_index} / {failure.artifact}"
                if chapter_index is not None
                else failure.artifact,
                failure.reason,
            )
            for failure in unit.failed_artifacts
        )
