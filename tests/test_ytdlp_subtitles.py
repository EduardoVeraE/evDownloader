"""Focused tests for yt-dlp-managed subtitle recovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yt_dlp

from evdownloader.config import Settings
from evdownloader.downloaders.base import Downloader
from evdownloader.downloaders.ytdlp import YtDlpDownloader
from evdownloader.models import Cookie, VideoSource


class UnsupportedDownloader(Downloader):
    async def download(self, source: VideoSource, dest: Path, settings: Settings) -> Path:
        return dest


def _staged_subtitle(ytdl: MagicMock, lang: str) -> Path:
    template = Path(ytdl.call_args.args[0]["outtmpl"])
    base_name = template.name.removesuffix(".%(ext)s")
    return template.parent / f"{base_name}.{lang}.vtt"


def _mock_ytdl(ytdl: MagicMock) -> MagicMock:
    instance = MagicMock()
    ytdl.return_value.__enter__.return_value = instance
    ytdl.return_value.__exit__.return_value = False
    return instance


def _source(**kwargs: object) -> VideoSource:
    return VideoSource(url="https://example.test/video", write_subs=True, **kwargs)


def test_managed_subtitle_capability_flags() -> None:
    assert Downloader.supports_managed_subtitles is False
    assert UnsupportedDownloader.supports_managed_subtitles is False
    assert YtDlpDownloader.supports_managed_subtitles is True


@pytest.mark.asyncio
async def test_downloader_default_observably_rejects_subtitle_recovery(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="does not support"):
        await UnsupportedDownloader().download_subtitles(
            VideoSource(url="https://example.test/video"),
            tmp_path / "lesson",
            Settings(download_dir=tmp_path),
        )


@pytest.mark.asyncio
async def test_download_subtitles_delegates_to_thread(tmp_path: Path) -> None:
    downloader = YtDlpDownloader()
    source = _source()
    settings = Settings(download_dir=tmp_path)
    expected = [tmp_path / "lesson.en.vtt"]

    with patch(
        "evdownloader.downloaders.ytdlp.asyncio.to_thread",
        new=AsyncMock(return_value=expected),
    ) as to_thread:
        result = await downloader.download_subtitles(source, tmp_path / "lesson", settings)

    assert result == expected
    to_thread.assert_awaited_once_with(
        downloader._run_subtitles, source, tmp_path / "lesson", settings
    )


@pytest.mark.asyncio
async def test_download_subtitles_stages_validates_and_publishes_only_this_attempt(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "01-Node.js"
    media = tmp_path / "01-Node.js.mp4"
    media.write_bytes(b"existing-media")
    unrelated = tmp_path / "01-Node.js.fr.vtt"
    unrelated.write_text("WEBVTT\n\nold", encoding="utf-8")
    source = _source(
        http_headers={"Referer": "https://example.test/"},
        cookie_jar=[Cookie(name="access_token", value="secret", domain=".example.test")],
        drm={"scheme": "widevine"},
    )
    settings = Settings(download_dir=tmp_path, use_drm=True, sub_langs="es,en", overwrite=False)
    staging_dir: Path | None = None
    cookiefile_path: Path | None = None

    with (
        patch("yt_dlp.YoutubeDL") as ytdl,
        patch.object(YtDlpDownloader, "_run_drm") as run_drm,
    ):
        instance = _mock_ytdl(ytdl)

        def download(_urls: list[str]) -> None:
            nonlocal cookiefile_path, staging_dir
            options = ytdl.call_args.args[0]
            template = Path(options["outtmpl"])
            staging_dir = template.parent
            cookiefile_path = Path(options["cookiefile"])
            assert staging_dir.parent == tmp_path
            assert staging_dir != tmp_path
            assert staging_dir.is_dir()
            assert cookiefile_path.exists()
            _staged_subtitle(ytdl, "es").write_bytes(b"\xef\xbb\xbf \nWEBVTT\n\nes")
            _staged_subtitle(ytdl, "en").write_text("\tWEBVTT\n\nen", encoding="utf-8")

        instance.download.side_effect = download
        result = await YtDlpDownloader().download_subtitles(source, dest, settings)

    options = ytdl.call_args.args[0]
    assert Path(options["outtmpl"]).parent != tmp_path
    assert Path(options["outtmpl"]).name == "01-Node.js.%(ext)s"
    assert options["overwrites"] is True
    assert options["skip_download"] is True
    assert options["writesubtitles"] is True
    assert options["subtitleslangs"] == ["es", "en"]
    assert options["subtitlesformat"] == "vtt/best"
    assert options["http_headers"] == source.http_headers
    assert "format" not in options
    assert "merge_output_format" not in options
    assert "postprocessors" not in options
    assert "allow_unplayable_formats" not in options
    assert result == [tmp_path / "01-Node.js.en.vtt", tmp_path / "01-Node.js.es.vtt"]
    assert unrelated.read_text(encoding="utf-8") == "WEBVTT\n\nold"
    assert media.read_bytes() == b"existing-media"
    assert staging_dir is not None and not staging_dir.exists()
    assert cookiefile_path is not None and not cookiefile_path.exists()
    run_drm.assert_not_called()
    instance.download.assert_called_once_with([source.url])


@pytest.mark.asyncio
async def test_download_subtitles_atomically_replaces_symlink_not_target(tmp_path: Path) -> None:
    external = tmp_path / "external.vtt"
    external.write_text("external-target", encoding="utf-8")
    final = tmp_path / "lesson.en.vtt"
    final.symlink_to(external)

    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = _mock_ytdl(ytdl)
        instance.download.side_effect = lambda _urls: _staged_subtitle(ytdl, "en").write_text(
            "WEBVTT\n\nnew", encoding="utf-8"
        )
        result = await YtDlpDownloader().download_subtitles(
            _source(), tmp_path / "lesson", Settings(download_dir=tmp_path)
        )

    assert result == [final]
    assert not final.is_symlink()
    assert final.read_text(encoding="utf-8") == "WEBVTT\n\nnew"
    assert external.read_text(encoding="utf-8") == "external-target"


@pytest.mark.asyncio
async def test_invalid_staged_vtt_publishes_nothing_and_is_sanitized(tmp_path: Path) -> None:
    final = tmp_path / "lesson.en.vtt"
    final.write_text("WEBVTT\n\nold", encoding="utf-8")
    staging_dir: Path | None = None
    secret = "signed-body-secret"

    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = _mock_ytdl(ytdl)

        def download(_urls: list[str]) -> None:
            nonlocal staging_dir
            staging_dir = Path(ytdl.call_args.args[0]["outtmpl"]).parent
            _staged_subtitle(ytdl, "en").write_text("WEBVTT\n\nnew", encoding="utf-8")
            _staged_subtitle(ytdl, "es").write_text(secret, encoding="utf-8")

        instance.download.side_effect = download
        with pytest.raises(RuntimeError) as exc_info:
            await YtDlpDownloader().download_subtitles(
                _source(), tmp_path / "lesson", Settings(download_dir=tmp_path)
            )

    assert str(exc_info.value) == "Managed subtitle recovery produced invalid WebVTT output"
    assert secret not in str(exc_info.value)
    assert final.read_text(encoding="utf-8") == "WEBVTT\n\nold"
    assert not (tmp_path / "lesson.es.vtt").exists()
    assert staging_dir is not None and not staging_dir.exists()


@pytest.mark.asyncio
async def test_download_subtitles_cleans_staging_and_cookiefile_after_error(
    tmp_path: Path,
) -> None:
    media = tmp_path / "lesson.mp4"
    media.write_bytes(b"existing-media")
    source = _source(
        cookie_jar=[Cookie(name="access_token", value="secret", domain=".example.test")]
    )
    staging_dir: Path | None = None
    cookiefile_path: Path | None = None

    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = _mock_ytdl(ytdl)

        def fail(_urls: list[str]) -> None:
            nonlocal cookiefile_path, staging_dir
            options = ytdl.call_args.args[0]
            staging_dir = Path(options["outtmpl"]).parent
            cookiefile_path = Path(options["cookiefile"])
            _staged_subtitle(ytdl, "en").write_text("WEBVTT\n\npartial", encoding="utf-8")
            raise yt_dlp.utils.DownloadError("subtitle failure")

        instance.download.side_effect = fail
        with pytest.raises(yt_dlp.utils.DownloadError, match="subtitle failure"):
            await YtDlpDownloader().download_subtitles(
                source, tmp_path / "lesson", Settings(download_dir=tmp_path)
            )

    assert staging_dir is not None and not staging_dir.exists()
    assert cookiefile_path is not None and not cookiefile_path.exists()
    assert not (tmp_path / "lesson.en.vtt").exists()
    assert media.read_bytes() == b"existing-media"
    instance.download.assert_called_once_with([source.url])


@pytest.mark.asyncio
async def test_download_subtitles_rejects_sources_not_managed_by_downloader(
    tmp_path: Path,
) -> None:
    with patch("yt_dlp.YoutubeDL") as ytdl, pytest.raises(ValueError, match="write_subs=True"):
        await YtDlpDownloader().download_subtitles(
            VideoSource(url="https://example.test/video"),
            tmp_path / "lesson",
            Settings(download_dir=tmp_path),
        )

    ytdl.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "settings", "expected_key", "expected_value"),
    [
        (
            _source(),
            Settings(cookies_from_browser="brave"),
            "cookiesfrombrowser",
            ("brave", None, None, None),
        ),
        (
            _source(
                http_headers={"Referer": "https://example.test/"},
                cookies={"session": "secret"},
            ),
            Settings(),
            "http_headers",
            {"Referer": "https://example.test/", "Cookie": "session=secret"},
        ),
    ],
)
async def test_download_subtitles_uses_normal_cookie_fallbacks(
    tmp_path: Path,
    source: VideoSource,
    settings: Settings,
    expected_key: str,
    expected_value: object,
) -> None:
    with patch("yt_dlp.YoutubeDL") as ytdl:
        _mock_ytdl(ytdl)
        result = await YtDlpDownloader().download_subtitles(source, tmp_path / "lesson", settings)

    assert result == []
    assert ytdl.call_args.args[0][expected_key] == expected_value


def test_normal_fallback_chooses_webm_over_manifest(tmp_path: Path) -> None:
    dest = tmp_path / "01-Node.js"
    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = _mock_ytdl(ytdl)

        def download(_urls: list[str]) -> None:
            (tmp_path / "01-Node.js.subtitles.json").write_text("{}", encoding="utf-8")
            (tmp_path / "01-Node.js.webm").write_bytes(b"media")

        instance.download.side_effect = download
        result = YtDlpDownloader()._run(
            VideoSource(url="https://example.test/video"),
            dest,
            Settings(download_dir=tmp_path),
        )

    assert ytdl.call_args.args[0]["outtmpl"] == str(dest) + ".%(ext)s"
    assert result == tmp_path / "01-Node.js.webm"


def test_normal_result_preserves_dotted_base_for_mp4(tmp_path: Path) -> None:
    dest = tmp_path / "01-Node.js"
    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = _mock_ytdl(ytdl)
        instance.download.side_effect = lambda _urls: (tmp_path / "01-Node.js.mp4").write_bytes(
            b"media"
        )
        result = YtDlpDownloader()._run(
            VideoSource(url="https://example.test/video"),
            dest,
            Settings(download_dir=tmp_path),
        )

    assert ytdl.call_args.args[0]["outtmpl"] == str(dest) + ".%(ext)s"
    assert result == tmp_path / "01-Node.js.mp4"


def test_normal_fallback_rejects_sidecar_only_result(tmp_path: Path) -> None:
    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = _mock_ytdl(ytdl)
        instance.download.side_effect = lambda _urls: (
            tmp_path / "lesson.subtitles.json"
        ).write_text("{}", encoding="utf-8")

        with pytest.raises(RuntimeError, match="no supported media file"):
            YtDlpDownloader()._run(
                VideoSource(url="https://example.test/video"),
                tmp_path / "lesson",
                Settings(download_dir=tmp_path),
            )


@pytest.mark.parametrize("prefix", [b"\f", b"\v"])
def test_webvtt_rejects_non_service_leading_whitespace(tmp_path: Path, prefix: bytes) -> None:
    subtitle = tmp_path / "lesson.en.vtt"
    subtitle.write_bytes(prefix + b"WEBVTT\n\ntext")

    assert YtDlpDownloader._valid_webvtt(subtitle) is False
