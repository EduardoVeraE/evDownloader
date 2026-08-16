"""Focused tests for native downloader output paths."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader.config import Settings
from evdownloader.downloaders.native import NativeDownloader
from evdownloader.models import Cookie, VideoSource


@pytest.mark.asyncio
async def test_native_output_preserves_dotted_logical_base(tmp_path: Path) -> None:
    downloader = NativeDownloader()
    source = VideoSource(url="https://example.test/master.m3u8")
    settings = Settings(download_dir=tmp_path)
    dest = tmp_path / "01-Node.js"
    expected = tmp_path / "01-Node.js.mp4"

    with patch.object(downloader, "_ffmpeg", new_callable=AsyncMock) as ffmpeg:
        result = await downloader.download(source, dest, settings)

    assert result == expected
    ffmpeg.assert_awaited_once_with(source.url, expected, source, settings)


@pytest.mark.asyncio
async def test_native_embed_scopes_cookie_and_closes_response_once() -> None:
    response = MagicMock()
    response.status_code.as_int.return_value = 200
    response.headers = {}
    response.text = AsyncMock(return_value='source: "https://cdn.example.test/video/master.m3u8"')
    response.close = AsyncMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    source = VideoSource(
        url="https://player.example.test/embed/id",
        is_embed=True,
        http_headers={"Authorization": "Bearer synthetic-secret"},
        cookie_jar=[Cookie(name="session", value="synthetic", domain=".example.test")],
        trusted_host_suffixes=("example.test",),
    )

    with patch("rnet.Client", return_value=client):
        result = await NativeDownloader()._resolve_m3u8(source)

    assert result == "https://cdn.example.test/video/master.m3u8"
    assert client.get.await_args.kwargs == {
        "headers": {"Cookie": "session=synthetic"},
        "allow_redirects": False,
    }
    response.close.assert_awaited_once_with()
