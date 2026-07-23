from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evdownloader.models import Subtitle, VideoSource
from evdownloader.service import _save_subtitles


@pytest.mark.asyncio
async def test_save_subtitles_counts_only_successful_tracks_per_language(
    tmp_path: Path,
) -> None:
    subtitles = [
        Subtitle(url="https://example.test/es/failed.vtt", lang="es"),
        Subtitle(url="https://example.test/en/one.vtt", lang="en"),
        Subtitle(url="https://example.test/es/one.vtt", lang="es"),
        Subtitle(url="https://example.test/en/two.vtt", lang="en"),
        Subtitle(url="https://example.test/es/two.vtt", lang="es"),
        Subtitle(url="https://example.test/es/three.vtt", lang="es"),
    ]
    contents = ["en one", "es one", "en two", "es two", "es three"]
    responses = []
    for content in contents:
        response = MagicMock()
        response.text = AsyncMock(return_value=content)
        responses.append(response)

    client = MagicMock()
    client.get = AsyncMock(side_effect=[ConnectionError("failed subtitle"), *responses])
    source = VideoSource(url="https://example.test/video", subtitles=subtitles)
    base = tmp_path / "lesson"

    with patch("evdownloader.service.rnet.Client", return_value=client):
        await _save_subtitles(subtitles, base, source)

    expected = {
        "lesson.es.vtt": "es one",
        "lesson.es-2.vtt": "es two",
        "lesson.es-3.vtt": "es three",
        "lesson.en.vtt": "en one",
        "lesson.en-2.vtt": "en two",
    }
    assert {path.name for path in tmp_path.iterdir()} == expected.keys()
    for filename, content in expected.items():
        assert (tmp_path / filename).read_text(encoding="utf-8") == content
