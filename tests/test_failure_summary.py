"""Final failure inventory, without leaking transport credentials."""

from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from evdownloader.config import Settings
from evdownloader.models import (
    Chapter,
    Course,
    Resource,
    ResourceKind,
    Subtitle,
    Unit,
    UnitExtras,
    UnitType,
    VideoSource,
)
from evdownloader.resource_download import ResourceDownloadResult
from evdownloader.service import _download_video, download_course


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 2])
async def test_final_inventory_lists_each_failure_and_excludes_successes(
    tmp_path: Path, limit: int | None
) -> None:
    units = [
        Unit(title=title, index=index, url=f"https://example.test/{index}", type=kind)
        for index, (title, kind) in enumerate(
            [
                ("Attachments", UnitType.LECTURE),
                ("Capture", UnitType.LECTURE),
                ("Broken media", UnitType.VIDEO),
                ("Captions", UnitType.VIDEO),
                ("Later success", UnitType.LECTURE),
            ],
            1,
        )
    ]
    course = Course(
        title="Course",
        url="https://example.test/course",
        chapters=[Chapter(title="Chapter", index=3, units=units)],
    )
    secret_url = "https://user:private@example.test/secret?token=private#private"
    extractor = MagicMock(needs_browser=False)
    extractor.name = "test"
    extractor.resource_host_suffixes = ("example.test",)

    async def extras(_ctx, unit, **_kwargs):
        if unit.title == "Capture":
            raise ValueError(f"Cookie: private {secret_url}")
        if unit.title == "Attachments":
            return UnitExtras(
                resources=[
                    Resource(title=title, url=secret_url, kind=ResourceKind.FILE)
                    for title in ("One.pdf", "Two.pdf", "Saved.pdf")
                ]
            )
        return UnitExtras()

    extractor.resolve_extras = AsyncMock(side_effect=extras)
    extractor.resolve_video = AsyncMock(
        return_value=VideoSource(
            url=secret_url,
            subtitles=[
                Subtitle(lang="es", url=secret_url),
                Subtitle(lang="es", url=secret_url),
                Subtitle(lang="en", url=secret_url),
            ],
        )
    )
    downloader = MagicMock()
    downloader.download = AsyncMock(
        side_effect=[
            RuntimeError(f"Authorization: private {secret_url}"),
            tmp_path / "captions.mp4",
        ]
    )
    stream = StringIO()
    with (
        patch("evdownloader.service.console", Console(file=stream, width=200)),
        patch("evdownloader.service.get_extractor", return_value=extractor),
        patch("evdownloader.service.get_downloader", return_value=downloader),
        patch("evdownloader.service._load_structure", new=AsyncMock(return_value=course)),
        patch(
            "evdownloader.service.download_resource",
            new=AsyncMock(
                side_effect=[
                    ResourceDownloadResult(False, "http_status", 403),
                    OSError(f"Cookie: private {secret_url}"),
                    ResourceDownloadResult(True),
                ]
            ),
        ),
        patch(
            "evdownloader.service._fetch_subtitle",
            new=AsyncMock(
                side_effect=[
                    (None, 403, None, "http_status"),
                    (b"invalid", 200, None, None),
                    (b"WEBVTT\n", 200, None, None),
                ]
            ),
        ),
        pytest.raises(RuntimeError, match="videos=1, extras=1, resources=2, subtitles=1"),
    ):
        await download_course(course.url, Settings(download_dir=tmp_path, limit=limit))

    text = stream.getvalue()
    summary = text.split("Archivos y recursos no descargados:", 1)[1]
    assert summary.count("  - ") == 6
    for identifier in (
        "One.pdf",
        "Two.pdf",
        "Capture",
        "Broken media",
        "subtítulo 1 (es)",
        "subtítulo 2 (es)",
    ):
        assert identifier in summary
    for reason in ("HTTP 403", "OSError", "ValueError", "RuntimeError", "invalid_vtt"):
        assert reason in summary
    assert summary.count("Capítulo 3") == 6
    for absent in (
        "Saved.pdf",
        "subtítulo 3",
        "Later success",
        "https://",
        "private",
        "Cookie:",
        "Authorization:",
    ):
        assert absent not in summary
    assert "private" not in text
    assert extractor.resolve_extras.await_count == (4 if limit else 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["managed", "missing_source", "resolution", "manifest", "prior_track"]
)
async def test_recovery_failures_identify_safe_resource(tmp_path: Path, mode: str) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"existing-media")
    if mode == "prior_track":
        base.with_suffix(".subtitles.json").write_text(
            '{"version":1,"complete":false,"files":["lesson.es.vtt","lesson.en.vtt"]}'
        )
    unit = Unit(title="Lesson", index=1, url="https://example.test/lesson")
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(
        return_value=(
            None if mode == "missing_source" else VideoSource(url=unit.url, write_subs=True)
        )
    )
    if mode == "resolution":
        extractor.resolve_video.side_effect = ValueError("token=private")
    downloader = MagicMock(supports_managed_subtitles=True)
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock(return_value=[])
    if mode == "managed":
        downloader.download_subtitles.side_effect = OSError("token=private")
    failures = []
    with patch("evdownloader.service._write_subtitle_manifest") as manifest:
        if mode == "manifest":
            manifest.side_effect = OSError("token=private")
        result = await _download_video(
            extractor,
            downloader,
            None,
            unit,
            base,
            Settings(download_dir=tmp_path),
            failed_artifacts=failures,
        )
    assert result == (True, False)
    assert len(failures) == (2 if mode == "prior_track" else 1)
    assert all("Lesson" in failure.artifact for failure in failures)
    assert "private" not in str(failures)
    downloader.download.assert_not_awaited()
    assert base.with_suffix(".mp4").read_bytes() == b"existing-media"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".mhtml", ".resumen.html", ".enlaces.md"])
async def test_capture_write_failure_lists_specific_artifact(tmp_path: Path, suffix: str) -> None:
    from evdownloader.service import _process_unit

    extras = UnitExtras(
        page_mhtml="MHTML" if suffix == ".mhtml" else None,
        summary_html="<p>Summary</p>" if suffix == ".resumen.html" else None,
        resources=[Resource(title="Link", url="https://example.test", kind=ResourceKind.LINK)]
        if suffix == ".enlaces.md"
        else [],
    )
    extractor = MagicMock()
    extractor.resolve_extras = AsyncMock(return_value=extras)
    with patch("evdownloader.service._write_text", new=AsyncMock(side_effect=OSError("private"))):
        result = await _process_unit(
            extractor,
            MagicMock(),
            None,
            Unit(title="Reading", url="https://example.test", type=UnitType.LECTURE),
            tmp_path,
            Settings(download_dir=tmp_path),
        )
    assert result.extras_failures == 1
    assert len(result.failed_artifacts) == 1
    assert suffix in result.failed_artifacts[0].artifact
    assert result.failed_artifacts[0].reason == "OSError"


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_result", ["error", "empty", "success"])
async def test_optional_platzi_capture_is_reported_without_changing_exit_policy(
    tmp_path: Path, capture_result: str
) -> None:
    from evdownloader.extractors.platzi import PlatziExtractor

    url = "https://platzi.com/clases/123-reading/"
    unit = Unit(title="Reading", index=1, url=url, type=UnitType.LECTURE)
    course = Course(title="Course", url=url, chapters=[Chapter(title="Chapter", units=[unit])])
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={"summaryHtml": "<p>Saved summary</p>"})
    page.close = AsyncMock()
    ctx = MagicMock()
    ctx.new_page = AsyncMock(return_value=page)
    client = MagicMock()
    client.send = AsyncMock(return_value={"data": "MHTML"} if capture_result == "success" else {})
    ctx.new_cdp_session = AsyncMock(return_value=client)
    if capture_result == "error":
        ctx.new_cdp_session.side_effect = RuntimeError("https://secret.test/?token=private")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=ctx)
    context.__aexit__ = AsyncMock(return_value=False)
    stream = StringIO()
    with (
        patch("evdownloader.service.console", Console(file=stream, width=200)),
        patch("evdownloader.service.get_extractor", return_value=PlatziExtractor()),
        patch("evdownloader.service.get_downloader", return_value=MagicMock()),
        patch("evdownloader.service.browser.browser_context", return_value=context),
        patch("evdownloader.service._load_structure", new=AsyncMock(return_value=course)),
    ):
        await download_course(url, Settings(download_dir=tmp_path))
    text = stream.getvalue()
    assert "Descarga finalizada" in text
    assert "private" not in text
    if capture_result == "success":
        assert "Archivos y recursos no descargados" not in text
        assert list(tmp_path.rglob("*.mhtml"))
    else:
        assert "Reading / .mhtml: page_capture_failed_or_unavailable" in text
        assert not list(tmp_path.rglob("*.mhtml"))
    assert len(list(tmp_path.rglob("*.resumen.html"))) == 1


def test_failure_labels_redact_urls_and_controls_but_preserve_literal_markup() -> None:
    from evdownloader.service import DownloadOutcome, _print_failed_artifacts, _record_failure

    failures = []
    _record_failure(
        failures, "[red]Notes[/red]\nhttps://user:private@host/path?token=secret", "http_status"
    )
    stream = StringIO()
    with patch("evdownloader.service.console", Console(file=stream, width=200)):
        _print_failed_artifacts(DownloadOutcome(failed_artifacts=failures))
    assert "[red]Notes[/red] <URL omitida>: http_status" in stream.getvalue()
    assert "private" not in stream.getvalue()
    assert "secret" not in stream.getvalue()
