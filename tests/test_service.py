from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from evdownloader.config import Settings
from evdownloader.models import Resource, ResourceKind, Subtitle, Unit, UnitExtras, VideoSource
from evdownloader.service import _download_files, _download_video, _save_extras


def _unit() -> Unit:
    return Unit(title="Lesson", url="https://example.test/lesson", index=1)


def _response(body: bytes = b"WEBVTT\n") -> MagicMock:
    response = MagicMock()
    response.status_code.is_success.return_value = True
    response.status_code.as_int.return_value = 200
    response.headers = {}
    response.bytes = AsyncMock(return_value=body)
    return response


def _manifest(base: Path) -> dict[str, object]:
    return json.loads((base.parent / f"{base.name}.subtitles.json").read_text(encoding="utf-8"))


def _write_manifest(base: Path, payload: object) -> None:
    (base.parent / f"{base.name}.subtitles.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def output() -> StringIO:
    stream = StringIO()
    with patch(
        "evdownloader.service.console",
        Console(file=stream, force_terminal=False, color_system=None),
    ):
        yield stream


@pytest.mark.asyncio
async def test_satisfied_manifest_uses_existing_fast_path(tmp_path: Path, output: StringIO) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    (tmp_path / "lesson.es.vtt").write_text("WEBVTT\n", encoding="utf-8")
    _write_manifest(
        base,
        {"version": 1, "complete": True, "files": ["lesson.es.vtt"]},
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock()
    downloader = MagicMock()
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock()

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    extractor.resolve_video.assert_not_awaited()
    downloader.download.assert_not_awaited()
    downloader.download_subtitles.assert_not_awaited()
    assert "Ya existe" in output.getvalue()


@pytest.mark.asyncio
async def test_dotted_base_uses_canonical_video_manifest_and_vtt_names(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "01-Node.js"
    canonical_video = tmp_path / "01-Node.js.mp4"
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[Subtitle(lang="es", url="https://cdn.example.test/es.vtt")],
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()

    async def download(_source: VideoSource, destination: Path, _settings: Settings) -> Path:
        assert destination == base
        canonical_video.write_bytes(b"media")
        return canonical_video

    downloader.download = AsyncMock(side_effect=download)
    client = MagicMock()
    client.get = AsyncMock(return_value=_response())

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _download_video(
            extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
        )

    assert canonical_video.read_bytes() == b"media"
    assert not (tmp_path / "01-Node.mp4").exists()
    assert (tmp_path / "01-Node.js.es.vtt").read_text(encoding="utf-8") == "WEBVTT\n"
    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": ["01-Node.js.es.vtt"],
    }


@pytest.mark.asyncio
async def test_dotted_base_legacy_video_recovers_subtitles_without_media_download(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "01-Node.js"
    legacy_video = tmp_path / "01-Node.mp4"
    legacy_video.write_bytes(b"legacy-media")
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock(return_value=[])

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    downloader.download.assert_not_awaited()
    downloader.download_subtitles.assert_awaited_once()
    assert legacy_video.read_bytes() == b"legacy-media"
    assert not (tmp_path / "01-Node.js.mp4").exists()
    assert _manifest(base) == {"version": 1, "complete": True, "files": []}


@pytest.mark.asyncio
async def test_dotted_base_legacy_video_uses_satisfied_fast_path(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "01-Node.js"
    legacy_video = tmp_path / "01-Node.mp4"
    legacy_video.write_bytes(b"legacy-media")
    _write_manifest(base, {"version": 1, "complete": True, "files": []})
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock()
    downloader = MagicMock()
    downloader.download = AsyncMock()

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    extractor.resolve_video.assert_not_awaited()
    downloader.download.assert_not_awaited()
    assert legacy_video.read_bytes() == b"legacy-media"
    assert "01-Node.mp4" in output.getvalue()


@pytest.mark.asyncio
async def test_dotted_base_service_extras_append_suffixes(tmp_path: Path) -> None:
    base = tmp_path / "01-Node.js"
    unit = _unit()
    file_resource = Resource(
        title="notes.txt",
        url="https://example.test/notes.txt",
        kind=ResourceKind.FILE,
    )
    extractor = MagicMock()
    extractor.resolve_extras = AsyncMock(
        return_value=UnitExtras(
            summary_html="<p>Summary</p>",
            page_mhtml="MHTML",
            resources=[
                Resource(
                    title="Docs",
                    url="https://example.test/docs",
                    kind=ResourceKind.LINK,
                ),
                file_resource,
            ],
        )
    )

    with patch("evdownloader.service._download_files", new_callable=AsyncMock) as download_files:
        await _save_extras(extractor, None, unit, base, Settings(download_dir=tmp_path))

    assert (tmp_path / "01-Node.js.resumen.html").exists()
    assert (tmp_path / "01-Node.js.mhtml").read_text(encoding="utf-8") == "MHTML"
    assert (tmp_path / "01-Node.js.enlaces.md").exists()
    assert not (tmp_path / "01-Node.resumen.html").exists()
    download_files.assert_awaited_once_with([file_resource], tmp_path / "01-Node.js-recursos", {})


@pytest.mark.asyncio
async def test_legacy_partial_vtt_is_rebuilt_without_media_download(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    media.write_bytes(b"original-media")
    (tmp_path / "lesson.es.vtt").write_text("old", encoding="utf-8")
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[
            Subtitle(lang="es", url="https://cdn.example.test/es.vtt"),
            Subtitle(lang="en", url="https://cdn.example.test/en.vtt"),
        ],
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(side_effect=[_response(b"WEBVTT\nes"), _response(b"WEBVTT\nen")])

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _download_video(
            extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
        )

    extractor.resolve_video.assert_awaited_once()
    downloader.download.assert_not_awaited()
    downloader.download_subtitles.assert_not_awaited()
    assert media.read_bytes() == b"original-media"
    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": ["lesson.es.vtt", "lesson.en.vtt"],
    }
    assert "Subtítulos recuperados" in output.getvalue()


@pytest.mark.asyncio
async def test_missing_manifested_file_triggers_recovery(tmp_path: Path, output: StringIO) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    (tmp_path / "lesson.es.vtt").write_text("WEBVTT\n", encoding="utf-8")
    _write_manifest(
        base,
        {
            "version": 1,
            "complete": True,
            "files": ["lesson.es.vtt", "lesson.en.vtt"],
        },
    )
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[Subtitle(lang="en", url="https://cdn.example.test/en.vtt")],
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.download = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=_response())

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _download_video(
            extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
        )

    extractor.resolve_video.assert_awaited_once()
    downloader.download.assert_not_awaited()
    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": ["lesson.es.vtt", "lesson.en.vtt"],
    }


@pytest.mark.asyncio
async def test_reduced_source_retains_prior_missing_expectation_and_stays_incomplete(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    _write_manifest(
        base,
        {
            "version": 1,
            "complete": False,
            "files": ["lesson.es.vtt", "lesson.en.vtt"],
        },
    )
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[Subtitle(lang="es", url="https://cdn.example.test/es.vtt")],
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.download = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=_response())

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _download_video(
            extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
        )

    assert _manifest(base) == {
        "version": 1,
        "complete": False,
        "files": ["lesson.es.vtt", "lesson.en.vtt"],
    }
    assert "missing_or_invalid_webvtt" in output.getvalue()


@pytest.mark.asyncio
async def test_direct_partial_failure_stays_incomplete_and_preserves_media(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    media.write_bytes(b"original-media")
    secret = "signed-secret"
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[
            Subtitle(lang="es", url="https://cdn.example.test/one.vtt"),
            Subtitle(lang="es", url=f"https://cdn.example.test/two.vtt?token={secret}"),
        ],
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.download = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(side_effect=[_response(), RuntimeError(f"request failed with {secret}")])

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _download_video(
            extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
        )

    assert media.read_bytes() == b"original-media"
    assert _manifest(base) == {
        "version": 1,
        "complete": False,
        "files": ["lesson.es.vtt", "lesson.es-2.vtt"],
    }
    assert not (tmp_path / "lesson.es-2.vtt").exists()
    assert secret not in output.getvalue()
    downloader.download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("emitted", ["lesson.en.vtt", None])
async def test_managed_recovery_accepts_tracks_or_verified_zero_tracks(
    tmp_path: Path, output: StringIO, emitted: str | None
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    media.write_bytes(b"original-media")
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()

    async def download_subtitles(*_args: object) -> list[Path]:
        if emitted is None:
            return []
        path = tmp_path / emitted
        path.write_text("WEBVTT\n", encoding="utf-8")
        return [path]

    downloader.download_subtitles = AsyncMock(side_effect=download_subtitles)

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    downloader.download_subtitles.assert_awaited_once_with(
        source, base, Settings(download_dir=tmp_path)
    )
    downloader.download.assert_not_awaited()
    assert media.read_bytes() == b"original-media"
    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": [] if emitted is None else [emitted],
    }


@pytest.mark.asyncio
async def test_managed_recovery_sanitizes_secret_bearing_exception(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock(
        side_effect=RuntimeError(
            "https://signed.example.test/sub?token=secret Cookie: private response-body"
        )
    )

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    text = output.getvalue()
    assert "RuntimeError" in text
    for secret in ("signed.example", "token=secret", "Cookie", "private", "response-body"):
        assert secret not in text
    assert _manifest(base) == {"version": 1, "complete": False, "files": []}


@pytest.mark.asyncio
async def test_unsupported_managed_downloader_cannot_complete_empty(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = False
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock()

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    downloader.download.assert_not_awaited()
    downloader.download_subtitles.assert_not_awaited()
    assert _manifest(base) == {"version": 1, "complete": False, "files": []}
    assert "managed_subtitles_unsupported" in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "corrupt", "symlink"])
async def test_invalid_manifested_webvtt_triggers_recovery(
    tmp_path: Path, output: StringIO, state: str
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    subtitle = tmp_path / "lesson.es.vtt"
    if state == "corrupt":
        subtitle.write_text("not-webvtt", encoding="utf-8")
    elif state == "symlink":
        external = tmp_path / "external.vtt"
        external.write_text("WEBVTT\n", encoding="utf-8")
        subtitle.symlink_to(external)
    _write_manifest(
        base,
        {"version": 1, "complete": True, "files": ["lesson.es.vtt"]},
    )
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock(return_value=[])

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    extractor.resolve_video.assert_awaited_once()
    downloader.download_subtitles.assert_awaited_once()
    assert _manifest(base) == {
        "version": 1,
        "complete": False,
        "files": ["lesson.es.vtt"],
    }


@pytest.mark.asyncio
async def test_managed_recovery_unions_prior_and_exact_returned_paths(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    prior = tmp_path / "lesson.es.vtt"
    prior.write_text("WEBVTT\nprior", encoding="utf-8")
    unrelated = tmp_path / "lesson.fr.vtt"
    unrelated.write_text("WEBVTT\nunrelated", encoding="utf-8")
    _write_manifest(
        base,
        {"version": 1, "complete": False, "files": [prior.name]},
    )
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()
    recovered = tmp_path / "lesson.en.vtt"

    async def download_subtitles(*_args: object) -> list[Path]:
        recovered.write_text("WEBVTT\nnew", encoding="utf-8")
        return [recovered]

    downloader.download_subtitles = AsyncMock(side_effect=download_subtitles)

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": ["lesson.es.vtt", "lesson.en.vtt"],
    }


@pytest.mark.asyncio
async def test_source_resolution_failure_is_sanitized_and_leaves_incomplete_manifest(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    media.write_bytes(b"original-media")
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(
        side_effect=ValueError("https://example.test/?token=secret Authorization: private")
    )
    downloader = MagicMock()
    downloader.download = AsyncMock()

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    text = output.getvalue()
    assert "ValueError" in text
    assert "token=secret" not in text
    assert "Authorization" not in text
    assert media.read_bytes() == b"original-media"
    assert _manifest(base) == {"version": 1, "complete": False, "files": []}
    downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_overwrite_runs_full_download_and_refreshes_direct_manifest(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    media.write_bytes(b"old-media")
    legacy = tmp_path / "lesson.legacy.vtt"
    legacy.write_text("legacy", encoding="utf-8")
    _write_manifest(
        base,
        {"version": 1, "complete": True, "files": ["lesson.legacy.vtt"]},
    )
    source = VideoSource(
        url="https://video.example.test/master.m3u8",
        subtitles=[Subtitle(lang="en", url="https://cdn.example.test/en.vtt")],
    )
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()

    async def download(*_args: object) -> Path:
        media.write_bytes(b"new-media")
        return media

    downloader.download = AsyncMock(side_effect=download)
    downloader.download_subtitles = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=_response())
    settings = Settings(download_dir=tmp_path, overwrite=True)

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _download_video(extractor, downloader, None, _unit(), base, settings)

    downloader.download.assert_awaited_once_with(source, base, settings)
    downloader.download_subtitles.assert_not_awaited()
    assert media.read_bytes() == b"new-media"
    assert legacy.read_text(encoding="utf-8") == "legacy"
    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": ["lesson.en.vtt"],
    }


@pytest.mark.asyncio
async def test_full_managed_download_records_matching_invalid_vtts_as_incomplete(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    corrupt = tmp_path / "lesson.fr.vtt"
    corrupt.write_text("not-webvtt", encoding="utf-8")
    external = tmp_path / "external.vtt"
    external.write_text("WEBVTT\nexternal", encoding="utf-8")
    symlink = tmp_path / "lesson.de.vtt"
    symlink.symlink_to(external)

    async def download(*_args: object) -> Path:
        media.write_bytes(b"media")
        (tmp_path / "lesson.es.vtt").write_text("WEBVTT\n", encoding="utf-8")
        return media

    downloader.download = AsyncMock(side_effect=download)
    downloader.download_subtitles = AsyncMock()

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    downloader.download_subtitles.assert_not_awaited()
    assert _manifest(base) == {
        "version": 1,
        "complete": False,
        "files": ["lesson.de.vtt", "lesson.es.vtt", "lesson.fr.vtt"],
    }
    assert "invalid_managed_subtitle" in output.getvalue()
    assert corrupt.read_text(encoding="utf-8") == "not-webvtt"
    assert symlink.is_symlink()


@pytest.mark.asyncio
async def test_corrupt_only_managed_output_is_incomplete_and_recovered_next_run(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = tmp_path / "lesson.mp4"
    corrupt = tmp_path / "lesson.es.vtt"
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True

    async def download(*_args: object) -> Path:
        media.write_bytes(b"media")
        corrupt.write_text("truncated", encoding="utf-8")
        return media

    async def download_subtitles(*_args: object) -> list[Path]:
        corrupt.write_text("WEBVTT\nrecovered", encoding="utf-8")
        return [corrupt]

    downloader.download = AsyncMock(side_effect=download)
    downloader.download_subtitles = AsyncMock(side_effect=download_subtitles)
    settings = Settings(download_dir=tmp_path)

    await _download_video(extractor, downloader, None, _unit(), base, settings)

    assert _manifest(base) == {
        "version": 1,
        "complete": False,
        "files": ["lesson.es.vtt"],
    }
    assert "invalid_managed_subtitle" in output.getvalue()

    await _download_video(extractor, downloader, None, _unit(), base, settings)

    assert extractor.resolve_video.await_count == 2
    downloader.download.assert_awaited_once()
    downloader.download_subtitles.assert_awaited_once_with(source, base, settings)
    assert _manifest(base) == {
        "version": 1,
        "complete": True,
        "files": ["lesson.es.vtt"],
    }


@pytest.mark.asyncio
async def test_final_manifest_write_failure_leaves_prior_incomplete_manifest(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    subtitle = tmp_path / "lesson.es.vtt"
    subtitle.write_text("WEBVTT\n", encoding="utf-8")
    _write_manifest(
        base,
        {"version": 1, "complete": False, "files": [subtitle.name]},
    )
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock(return_value=[])
    manifest_path = tmp_path / "lesson.subtitles.json"
    real_replace = os.replace
    manifest_replaces = 0

    def fail_final_manifest(source_path: str, destination: Path) -> None:
        nonlocal manifest_replaces
        if Path(destination) == manifest_path:
            manifest_replaces += 1
            if manifest_replaces == 2:
                raise OSError("final manifest failed with secret")
        real_replace(source_path, destination)

    with patch("evdownloader.service.os.replace", side_effect=fail_final_manifest):
        await _download_video(
            extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
        )

    assert _manifest(base) == {
        "version": 1,
        "complete": False,
        "files": ["lesson.es.vtt"],
    }
    assert "OSError" in output.getvalue()
    assert "secret" not in output.getvalue()
    assert not list(tmp_path.glob(".lesson.subtitles.json.*.tmp"))


@pytest.mark.asyncio
async def test_direct_source_without_captured_tracks_does_not_claim_complete(
    tmp_path: Path, output: StringIO
) -> None:
    base = tmp_path / "lesson"
    media = base.with_suffix(".mp4")
    source = VideoSource(url="https://example.test/video")
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.download = AsyncMock(return_value=media)

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    assert _manifest(base) == {"version": 1, "complete": False, "files": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"version": 2, "complete": True, "files": []},
        {"version": 1, "complete": 1, "files": []},
        {"version": 1, "complete": True, "files": ["../lesson.es.vtt"]},
        {"version": 1, "complete": True, "files": ["/tmp/lesson.es.vtt"]},
        {"version": 1, "complete": True, "files": ["other.es.vtt"]},
        {"version": 1, "complete": True, "files": ["lesson.es\x00.vtt"]},
        {"version": 1, "complete": True, "files": ["lesson.es\x1f.vtt"]},
        {"version": 1, "complete": True, "files": ["lesson.es\x85.vtt"]},
    ],
)
async def test_invalid_or_traversal_manifest_is_treated_as_legacy(
    tmp_path: Path, output: StringIO, payload: object
) -> None:
    base = tmp_path / "lesson"
    base.with_suffix(".mp4").write_bytes(b"media")
    manifest_path = tmp_path / "lesson.subtitles.json"
    manifest_path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    source = VideoSource(url="https://example.test/video", write_subs=True)
    extractor = MagicMock()
    extractor.resolve_video = AsyncMock(return_value=source)
    downloader = MagicMock()
    downloader.supports_managed_subtitles = True
    downloader.download = AsyncMock()
    downloader.download_subtitles = AsyncMock(return_value=[])

    await _download_video(
        extractor, downloader, None, _unit(), base, Settings(download_dir=tmp_path)
    )

    extractor.resolve_video.assert_awaited_once()
    downloader.download.assert_not_awaited()
    downloader.download_subtitles.assert_awaited_once()
    assert _manifest(base) == {"version": 1, "complete": True, "files": []}


@pytest.mark.asyncio
async def test_resource_download_uses_default_client_timeouts(tmp_path: Path) -> None:
    response = MagicMock()
    response.bytes = AsyncMock(return_value=b"resource")
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    resource = Resource(
        title="notes.txt",
        url="https://example.test/notes.txt",
        kind=ResourceKind.FILE,
    )

    with patch("evdownloader.service.rnet.Client", return_value=client) as client_factory:
        await _download_files([resource], tmp_path, {})

    assert set(client_factory.call_args.kwargs) == {"impersonate"}
    assert (tmp_path / "notes.txt").read_bytes() == b"resource"
