"""Tests de las funciones puras del extractor de Udemy.

Cubren, sin red:
* ``supports`` — enrutado por dominio.
* ``_classify_unit`` — partición de equivalencia video / article / quiz.
* ``_build_course`` — dict crudo del DOM -> ``Course`` (dedup, índices, títulos).
* La separación de sesión por plataforma (``config.session_file``).
* Los regex/señales que sostienen la heurística de DRM.
"""

from __future__ import annotations

from video_downloader.config import session_file
from video_downloader.extractors import get_extractor, get_extractor_by_name
from video_downloader.extractors.udemy import (
    _DRM_URL_SIGNALS,
    _M3U8_RE,
    _MPD_RE,
    _VTT_RE,
    UdemyExtractor,
)
from video_downloader.models import UnitType


# -- Enrutado -----------------------------------------------------------------
def test_supports_reconoce_udemy() -> None:
    assert UdemyExtractor.supports("https://www.udemy.com/course/foo/")
    assert not UdemyExtractor.supports("https://platzi.com/cursos/foo/")


def test_get_extractor_por_url_devuelve_udemy() -> None:
    assert isinstance(get_extractor("https://www.udemy.com/course/x/"), UdemyExtractor)


def test_get_extractor_por_nombre() -> None:
    assert isinstance(get_extractor_by_name("udemy"), UdemyExtractor)


# -- Clasificación de tipo (sin navegar) --------------------------------------
def test_clasifica_quiz_por_url() -> None:
    assert UdemyExtractor._classify_unit("/course/x/learn/quiz/123", "", "") is UnitType.QUIZ


def test_clasifica_quiz_por_icono() -> None:
    assert (
        UdemyExtractor._classify_unit("/course/x/learn/lecture/1", "udi udi-quiz", "")
        is UnitType.QUIZ
    )


def test_clasifica_video_por_icono() -> None:
    assert (
        UdemyExtractor._classify_unit("/course/x/learn/lecture/1", "udi udi-video", "")
        is UnitType.VIDEO
    )


def test_clasifica_video_por_duracion() -> None:
    # Sin icono, un badge de duración mm:ss delata una clase de video.
    assert (
        UdemyExtractor._classify_unit("/course/x/learn/lecture/1", "", "05:32")
        is UnitType.VIDEO
    )


def test_clasifica_article_por_icono() -> None:
    assert (
        UdemyExtractor._classify_unit("/course/x/learn/lecture/2", "udi udi-article", "1 min")
        is UnitType.LECTURE
    )


def test_clasifica_lecture_sin_senales() -> None:
    assert (
        UdemyExtractor._classify_unit("/course/x/learn/lecture/3", "", "") is UnitType.LECTURE
    )


# -- Construcción del curso ---------------------------------------------------
def test_build_course_estructura_indices_y_absolutiza_href() -> None:
    raw = {
        "title": "Curso de Prueba",
        "chapters": [
            {
                "title": "Introducción",
                "units": [
                    {
                        "href": "/course/x/learn/lecture/10",
                        "title": "Bienvenida",
                        "kind": "udi-video",
                        "duration": "03:00",
                    },
                    {
                        "href": "/course/x/learn/quiz/11",
                        "title": "Quiz 1",
                        "kind": "udi-quiz",
                        "duration": "",
                    },
                ],
            }
        ],
    }
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", raw)

    assert course.title == "Curso de Prueba"
    assert len(course.chapters) == 1
    ch = course.chapters[0]
    assert ch.title == "Introducción"
    assert ch.index == 1
    assert [u.index for u in ch.units] == [1, 2]
    assert ch.units[0].url == "https://www.udemy.com/course/x/learn/lecture/10"
    assert ch.units[0].type is UnitType.VIDEO
    assert ch.units[1].type is UnitType.QUIZ


def test_build_course_deduplica_hrefs_repetidos() -> None:
    raw = {
        "title": "C",
        "chapters": [
            {
                "title": "S1",
                "units": [
                    {
                        "href": "/course/x/learn/lecture/1",
                        "title": "A",
                        "kind": "udi-video",
                        "duration": "01:00",
                    },
                    {
                        "href": "/course/x/learn/lecture/1",
                        "title": "A dup",
                        "kind": "udi-video",
                        "duration": "01:00",
                    },
                ],
            }
        ],
    }
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", raw)
    assert sum(len(c.units) for c in course.chapters) == 1


def test_build_course_salta_capitulos_sin_unidades() -> None:
    raw = {"title": "C", "chapters": [{"title": "Vacío", "units": []}]}
    course = UdemyExtractor()._build_course("https://www.udemy.com/course/x/", raw)
    assert course.chapters == []


# -- Señales de red (heurística DRM vs HLS) -----------------------------------
def test_m3u8_captura_master_hls() -> None:
    url = "https://www.udemy.com/assets/123/encrypted-files/out/v1/master.m3u8?token=abc"
    assert _M3U8_RE.search(url) is not None


def test_m3u8_ignora_playlist_de_subtitulos() -> None:
    assert _M3U8_RE.search("https://cdn.udemy.com/x/captions.vtt.m3u8") is None


def test_mpd_detecta_dash_drm() -> None:
    assert _MPD_RE.search("https://www.udemy.com/assets/123/encrypted/index.mpd") is not None


def test_vtt_captura_subtitulos() -> None:
    assert _VTT_RE.search("https://cdn.udemy.com/x/en_US.vtt") is not None


def test_url_de_licencia_widevine_es_senal_drm() -> None:
    lic = "https://www.udemy.com/api-2.0/media-license/widevine/abc/"
    assert any(sig in lic.lower() for sig in _DRM_URL_SIGNALS)


# -- Sesión por plataforma ----------------------------------------------------
def test_session_file_separa_por_plataforma() -> None:
    p_platzi = session_file("platzi")
    p_udemy = session_file("udemy")
    assert p_platzi != p_udemy
    assert p_platzi.name == "session-platzi.json"
    assert p_udemy.name == "session-udemy.json"
