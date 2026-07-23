"""Focused tests for native downloader output paths."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evdownloader.config import Settings
from evdownloader.downloaders.native import NativeDownloader
from evdownloader.models import VideoSource


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
