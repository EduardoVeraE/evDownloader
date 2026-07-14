"""Pruebas de selección de cookiefile para yt-dlp."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from evdownloader.config import Settings
from evdownloader.downloaders.ytdlp import YtDlpDownloader
from evdownloader.models import Cookie, VideoSource


def test_ytdlp_usa_cookiefile_de_la_fuente_resuelta_y_lo_borra(tmp_path: Path) -> None:
    source = VideoSource(
        url="https://www.udemy.com/course/example/",
        cookie_jar=[Cookie(name="access_token", value="not-for-output", domain=".udemy.com")],
    )
    exists_during_download = False
    has_cookie = False
    cookiefile_path: Path | None = None

    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = MagicMock()
        ytdl.return_value.__enter__.return_value = instance
        ytdl.return_value.__exit__.return_value = False

        def download(_urls) -> None:
            nonlocal cookiefile_path, exists_during_download, has_cookie
            cookiefile = Path(ytdl.call_args.args[0]["cookiefile"])
            exists_during_download = cookiefile.exists()
            has_cookie = "access_token" in cookiefile.read_text(encoding="utf-8")
            cookiefile_path = cookiefile

        instance.download.side_effect = download
        YtDlpDownloader()._run(source, tmp_path / "video", Settings(download_dir=tmp_path))

    assert exists_during_download is True
    assert has_cookie is True
    assert cookiefile_path is not None
    assert not cookiefile_path.exists()


def test_ytdlp_conserva_fallback_explicito_al_navegador(tmp_path: Path) -> None:
    source = VideoSource(url="https://www.udemy.com/course/example/")

    with patch("yt_dlp.YoutubeDL") as ytdl:
        instance = MagicMock()
        ytdl.return_value.__enter__.return_value = instance
        ytdl.return_value.__exit__.return_value = False
        YtDlpDownloader()._run(
            source,
            tmp_path / "video",
            Settings(download_dir=tmp_path, cookies_from_browser="brave"),
        )

    options = ytdl.call_args.args[0]
    assert options["cookiesfrombrowser"] == ("brave", None, None, None)
