"""Tests de los patrones de extracción de Platzi/Mediastream.

Verifica el fix de la causa raíz: el regex de video NO debe capturar los
playlists de subtítulos ``.vtt.m3u8``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evdownloader.extractors.platzi import (
    _M3U8_RE,
    _MDSTRM_EMBED_RE,
    _VTT_RE,
    PlatziExtractor,
)
from evdownloader.models import Unit, UnitType


def test_clasifica_video_por_miniatura_mdstrm() -> None:
    thumb = "https://thumbs.cdn.mdstrm.com/thumbs/abc/thumb_x_9s.jpg"
    assert PlatziExtractor._classify_unit("/cursos/x/clase/", thumb, "") is UnitType.VIDEO


def test_clasifica_video_por_duracion_sin_miniatura() -> None:
    # Si la miniatura no cargó pero hay badge de duración, sigue siendo video.
    assert PlatziExtractor._classify_unit("/cursos/x/clase/", "", "08:15 min") is UnitType.VIDEO


def test_clasifica_quiz_por_url() -> None:
    assert PlatziExtractor._classify_unit("/cursos/x/quiz/123/", "", "") is UnitType.QUIZ


def test_clasifica_lecture_sin_miniatura_ni_duracion() -> None:
    assert PlatziExtractor._classify_unit("/cursos/x/lectura/", "", "") is UnitType.LECTURE


def test_m3u8_ignora_subtitulos_vtt_m3u8() -> None:
    sub = "https://mdstrm.com/video/abc/subs.vtt.m3u8"
    assert _M3U8_RE.search(sub) is None


def test_m3u8_captura_master_real() -> None:
    url = "https://mdstrm.com/video/abc/master.m3u8?at=web-app&access_token=xyz"
    m = _M3U8_RE.search(url)
    assert m is not None
    assert m.group(0).startswith("https://mdstrm.com")


def test_vtt_captura_subtitulos() -> None:
    assert _VTT_RE.search("https://cdn.mdstrm.com/x/es.vtt") is not None


def test_embed_extrae_id() -> None:
    m = _MDSTRM_EMBED_RE.search("https://mdstrm.com/embed/5f3a1b2c3d?foo=bar")
    assert m is not None
    assert m.group(1) == "5f3a1b2c3d"


@pytest.mark.asyncio
async def test_resolve_video_captura_vtt_solicitado_tarde() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.listener = None
            self.waits = 0

        def on(self, event: str, listener) -> None:
            assert event == "request"
            self.listener = listener

        async def goto(self, url: str, wait_until: str) -> None:
            assert url == "https://platzi.com/clase/video/"
            assert wait_until == "domcontentloaded"

        async def wait_for_event(self, event: str, predicate, timeout: int):
            assert event == "request"
            self.waits += 1
            url = (
                "https://mdstrm.com/embed/video-1"
                if self.waits == 1
                else "https://cdn.mdstrm.com/subtitles/es-delayed.vtt"
            )
            request = SimpleNamespace(url=url)
            assert predicate(request)
            assert self.listener is not None
            self.listener(request)
            return request

        async def wait_for_timeout(self, timeout: int) -> None:
            assert timeout == 1500

        def remove_listener(self, event: str, listener) -> None:
            assert event == "request"
            assert listener is self.listener
            self.listener = None

        async def close(self) -> None:
            assert self.listener is None

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        async def new_page(self) -> FakePage:
            return self.page

        async def cookies(self) -> list[dict[str, str]]:
            return []

    ctx = FakeContext()
    unit = Unit(
        title="Video",
        url="https://platzi.com/clase/video/",
        type=UnitType.VIDEO,
    )

    source = await PlatziExtractor().resolve_video(ctx, unit)

    assert source is not None
    assert source.subtitles[0].url == "https://cdn.mdstrm.com/subtitles/es-delayed.vtt"
