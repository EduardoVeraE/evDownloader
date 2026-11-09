"""Offline provider regressions for the extracted shared-service boundaries."""

from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from evdownloader import artifacts, service
from evdownloader.config import Settings
from evdownloader.extractors.platzi import PlatziExtractor
from evdownloader.extractors.udemy import UdemyExtractor
from evdownloader.models import Chapter, Cookie, Course, Unit, UnitExtras, UnitType, VideoSource
from evdownloader.results import DownloadOutcome, FailedArtifact, UnitOutcome, VideoOutcome
from evdownloader.subtitle_transfer import SubtitleFetchResult, fetch_subtitle


@pytest.mark.parametrize(
    "unit,expected",
    [
        (UnitOutcome(), False),
        (UnitOutcome(video_failures=1), True),
        (UnitOutcome(extras_failures=1), True),
        (UnitOutcome(resource_failures=1), True),
        (UnitOutcome(subtitle_failures=1), True),
        (
            UnitOutcome(failed_artifacts=(FailedArtifact("optional.mhtml", "capture_failed"),)),
            False,
        ),
    ],
)
def test_result_policy_preserves_strict_failures_and_optional_observations(
    unit: UnitOutcome, expected: bool
) -> None:
    result = DownloadOutcome()
    result.add(unit, chapter_index=3)
    assert result.incomplete is expected
    assert result.summary == (
        f"videos={unit.video_failures}, extras={unit.extras_failures}, "
        f"resources={unit.resource_failures}, subtitles={unit.subtitle_failures}"
    )
    assert len(result.failed_artifacts) == len(unit.failed_artifacts)
    if unit.failed_artifacts:
        assert result.failed_artifacts[0].artifact == "Capítulo 3 / optional.mhtml"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["udemy", "platzi"])
@pytest.mark.parametrize("failure", [None, "media", "publication", "optional_capture"])
async def test_provider_pipeline_preserves_failure_and_publication_policy(
    tmp_path: Path, provider: str, failure: str | None
) -> None:
    extractor = UdemyExtractor() if provider == "udemy" else PlatziExtractor()
    unit = Unit(title="Lesson.with.dots", url=f"https://{provider}.com/lesson", index=1)
    course = Course(
        title="Course", url=unit.url, chapters=[Chapter(title="Chapter", index=2, units=[unit])]
    )
    source = VideoSource(url=unit.url, subtitle_inventory_confirmed=True)
    downloader = AsyncMock()
    downloader.download.return_value = tmp_path / "media.mp4"
    if failure == "media":
        downloader.download.side_effect = OSError("private transport details")
    extras = UnitExtras(
        summary_html="<p>Summary</p>", page_capture_failed=failure == "optional_capture"
    )
    stream = StringIO()
    with (
        patch.object(extractor, "resolve_video", new=AsyncMock(return_value=source)),
        patch.object(extractor, "resolve_extras", new=AsyncMock(return_value=extras)),
        patch("evdownloader.service._load_structure", new=AsyncMock(return_value=course)),
        patch("evdownloader.service.console", Console(file=stream)),
        patch(
            "evdownloader.artifacts.os.replace",
            side_effect=OSError("private publication details")
            if failure == "publication"
            else None,
            wraps=None if failure == "publication" else artifacts.os.replace,
        ),
    ):
        outcome = await service._run_download(
            extractor, downloader, None, unit.url, Settings(download_dir=tmp_path), use_cache=False
        )
    assert outcome.incomplete is (failure in {"media", "publication"})
    assert outcome.video_failures == int(failure in {"media", "publication"})
    assert outcome.extras_failures == 0
    assert "private" not in stream.getvalue()
    assert not list(tmp_path.rglob("*.tmp"))
    if failure == "optional_capture":
        assert len(outcome.failed_artifacts) == 1
        assert outcome.failed_artifacts[0].reason == "page_capture_failed_or_unavailable"
    summary_files = list(tmp_path.rglob("*.resumen.html"))
    assert len(summary_files) == 1
    assert "Lesson.with.dots.resumen.html" in summary_files[0].name


@pytest.mark.asyncio
async def test_course_limit_still_stops_before_later_nonvideo_extras(tmp_path: Path) -> None:
    from evdownloader.orchestration import run_course

    units = [
        Unit(title="Video", url="https://example.test/video", index=1),
        Unit(title="Reading", url="https://example.test/reading", index=2, type=UnitType.LECTURE),
    ]
    course = Course(title="Course", url=units[0].url, chapters=[Chapter(title="C", units=units)])
    process = AsyncMock(return_value=UnitOutcome())
    result = await run_course(
        course, "udemy", Settings(download_dir=tmp_path, limit=1), process, Console(file=StringIO())
    )
    assert not result.incomplete
    assert process.await_count == 1
    assert process.await_args.args[0] is units[0]


def test_atomic_publication_failure_preserves_prior_file_and_cleans_staging(tmp_path: Path) -> None:
    path = tmp_path / "lesson.es.vtt"
    path.write_text("WEBVTT\nold\n")
    with (
        patch("evdownloader.artifacts.os.replace", side_effect=OSError("publication failed")),
        pytest.raises(OSError),
    ):
        artifacts.publish_atomic_text(path, "WEBVTT\nnew\n")
    assert path.read_text() == "WEBVTT\nold\n"
    assert list(tmp_path.iterdir()) == [path]


def test_legacy_service_result_imports_and_tuple_contract_remain_compatible() -> None:
    assert service.DownloadOutcome is DownloadOutcome
    assert service.UnitOutcome is UnitOutcome
    result = VideoOutcome(True, False)
    assert result == (True, False)
    assert result.video_ok and not result.subtitles_ok


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["udemy.com", "platzi.com"])
@pytest.mark.parametrize("external_redirect", [False, True])
async def test_provider_subtitle_transfer_keeps_credentials_within_authority(
    provider: str, external_redirect: bool
) -> None:
    # All credentials and responses here are synthetic; no browser/session is read.
    response = MagicMock()
    response.status_code.as_int.return_value = 302 if external_redirect else 200
    response.status_code.is_success.return_value = not external_redirect
    response.headers = (
        {"location": "https://external.test/caption.vtt"} if external_redirect else {}
    )
    response.bytes = AsyncMock(return_value=b"WEBVTT\n")
    response.close = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    source = VideoSource(
        url=f"https://{provider}/media",
        trusted_host_suffixes=(provider,),
        http_headers={"Authorization": "synthetic", "X-Public": "allowed"},
        cookie_jar=[Cookie(name="session", value="synthetic", domain=provider, secure=True)],
    )
    result = await fetch_subtitle(client, f"https://{provider}/caption.vtt", source)
    assert isinstance(result, SubtitleFetchResult)
    assert result.failure == ("untrusted_host" if external_redirect else None)
    assert result.data == (None if external_redirect else b"WEBVTT\n")
    client.get.assert_awaited_once()
    headers = client.get.await_args.kwargs["headers"]
    assert headers == {"X-Public": "allowed", "Cookie": "session=synthetic"}
    assert client.get.await_args.kwargs["allow_redirects"] is False
    response.close.assert_awaited_once()
