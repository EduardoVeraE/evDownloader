"""Tests de los patrones de extracción de Platzi/Mediastream.

Verifica el fix de la causa raíz: el regex de video NO debe capturar los
playlists de subtítulos ``.vtt.m3u8``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from evdownloader.extractors.platzi import (
    _M3U8_RE,
    _MDSTRM_EMBED_RE,
    _VTT_RE,
    PlatziExtractor,
    _is_allowed_vtt_url,
)
from evdownloader.models import Unit, UnitType


class FakePage:
    def __init__(
        self,
        *,
        goto_requests: tuple[str, ...],
        waited_media: str | None = None,
        waited_vtt: str | None = None,
        media_wait_error: Exception | None = None,
        vtt_wait_error: Exception | None = None,
        trailing_requests: tuple[str, ...] = (),
        remove_error: Exception | None = None,
    ) -> None:
        self.listener = None
        self.events: list[str] = []
        self.goto_requests = goto_requests
        self.waited_media = waited_media
        self.waited_vtt = waited_vtt
        self.media_wait_error = media_wait_error
        self.vtt_wait_error = vtt_wait_error
        self.trailing_requests = trailing_requests
        self.remove_error = remove_error

    def on(self, event: str, listener) -> None:
        assert event == "request"
        self.listener = listener
        self.events.append("listener:on")

    def _emit(self, url: str) -> None:
        assert self.listener is not None
        self.listener(SimpleNamespace(url=url))

    async def goto(self, url: str, wait_until: str) -> None:
        assert url == "https://platzi.com/clase/video/"
        assert wait_until == "domcontentloaded"
        self.events.append("goto")
        for request_url in self.goto_requests:
            self._emit(request_url)

    async def wait_for_event(self, event: str, predicate, timeout: int):
        assert event == "request"
        if timeout == 20000:
            self.events.append("media-wait:20000")
            if self.media_wait_error:
                raise self.media_wait_error
            request = SimpleNamespace(url=self.waited_media)
        else:
            assert timeout == 6500
            self.events.append("vtt-wait:6500")
            if self.vtt_wait_error:
                raise self.vtt_wait_error
            request = SimpleNamespace(url=self.waited_vtt)
        assert predicate(request)
        return request

    async def wait_for_timeout(self, timeout: int) -> None:
        assert timeout == 1500
        self.events.append("trailing:1500")
        for request_url in self.trailing_requests:
            self._emit(request_url)

    def remove_listener(self, event: str, listener) -> None:
        assert event == "request"
        assert listener is self.listener
        self.events.append("listener:off")
        if self.remove_error:
            raise self.remove_error
        self.listener = None

    async def close(self) -> None:
        self.events.append("close")


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page

    async def cookies(self) -> list[dict[str, str]]:
        return []


async def resolve_with(page: FakePage):
    unit = Unit(
        title="Video",
        url="https://platzi.com/clase/video/",
        type=UnitType.VIDEO,
    )
    return await PlatziExtractor().resolve_video(FakeContext(page), unit)


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
    assert _VTT_RE.search("https://cdn.mdstrm.com/x/es.vtt?token=abc") is not None


def test_vtt_ignora_playlist_vtt_m3u8() -> None:
    assert _VTT_RE.search("https://cdn.mdstrm.com/x/es.vtt.m3u8") is None


def test_vtt_valida_limites_de_host() -> None:
    assert _is_allowed_vtt_url("https://cdn.mdstrm.com/x/es.vtt?token=abc")
    assert _is_allowed_vtt_url("https://static.platzi.com/x/es.vtt")
    assert not _is_allowed_vtt_url("https://mdstrm.com.evil.example/x/es.vtt")
    assert not _is_allowed_vtt_url("https://example.com/x/es.vtt")


def test_embed_extrae_id() -> None:
    m = _MDSTRM_EMBED_RE.search("https://mdstrm.com/embed/5f3a1b2c3d?foo=bar")
    assert m is not None
    assert m.group(1) == "5f3a1b2c3d"


@pytest.mark.asyncio
async def test_resolve_video_vtt_temprano_espera_media_antes_de_colectar() -> None:
    page = FakePage(
        goto_requests=("https://cdn.mdstrm.com/subtitles/es-early.vtt",),
        waited_media="https://mdstrm.com/embed/video-1",
        trailing_requests=("https://cdn.mdstrm.com/subtitles/en-trailing.vtt",),
    )

    source = await resolve_with(page)

    assert source is not None
    assert source.url == "https://mdstrm.com/embed/video-1"
    assert [subtitle.url for subtitle in source.subtitles] == [
        "https://cdn.mdstrm.com/subtitles/en-trailing.vtt",
        "https://cdn.mdstrm.com/subtitles/es-early.vtt",
    ]
    assert [subtitle.lang for subtitle in source.subtitles] == ["es", "es"]
    assert page.events == [
        "listener:on",
        "goto",
        "media-wait:20000",
        "trailing:1500",
        "listener:off",
        "close",
    ]


@pytest.mark.asyncio
async def test_resolve_video_media_tardia_espera_vtt_y_colecta_rafaga() -> None:
    media_url = "https://mdstrm.com/video/abc/master.m3u8"
    page = FakePage(
        goto_requests=(),
        waited_media=media_url,
        waited_vtt="https://cdn.mdstrm.com/subtitles/pt-first.vtt",
        trailing_requests=("https://cdn.mdstrm.com/subtitles/es-trailing.vtt",),
    )

    source = await resolve_with(page)

    assert source is not None
    assert source.url == media_url
    assert [subtitle.url for subtitle in source.subtitles] == [
        "https://cdn.mdstrm.com/subtitles/es-trailing.vtt",
        "https://cdn.mdstrm.com/subtitles/pt-first.vtt",
    ]
    assert page.events == [
        "listener:on",
        "goto",
        "media-wait:20000",
        "vtt-wait:6500",
        "trailing:1500",
        "listener:off",
        "close",
    ]


@pytest.mark.asyncio
async def test_resolve_video_media_en_goto_sin_vtt_no_colecta() -> None:
    media_url = "https://mdstrm.com/video/abc/master.m3u8"
    page = FakePage(
        goto_requests=(media_url,),
        vtt_wait_error=PlaywrightTimeoutError("no subtitles"),
    )

    source = await resolve_with(page)

    assert source is not None
    assert source.url == media_url
    assert source.subtitles == []
    assert page.events == [
        "listener:on",
        "goto",
        "vtt-wait:6500",
        "listener:off",
        "close",
    ]


@pytest.mark.asyncio
async def test_resolve_video_media_y_vtt_en_goto_solo_colecta_rafaga() -> None:
    page = FakePage(
        goto_requests=(
            "https://mdstrm.com/embed/video-1",
            "https://cdn.mdstrm.com/subtitles/es-early.vtt",
        ),
        trailing_requests=("https://cdn.mdstrm.com/subtitles/en-trailing.vtt",),
    )

    source = await resolve_with(page)

    assert source is not None
    assert len(source.subtitles) == 2
    assert page.events == [
        "listener:on",
        "goto",
        "trailing:1500",
        "listener:off",
        "close",
    ]


@pytest.mark.asyncio
async def test_resolve_video_cierra_pagina_si_falla_remove_listener() -> None:
    page = FakePage(
        goto_requests=(
            "https://mdstrm.com/embed/video-1",
            "https://cdn.mdstrm.com/subtitles/es.vtt",
        ),
        remove_error=RuntimeError("remove failed"),
    )

    with pytest.raises(RuntimeError, match="remove failed"):
        await resolve_with(page)

    assert page.events == [
        "listener:on",
        "goto",
        "trailing:1500",
        "listener:off",
        "close",
    ]
